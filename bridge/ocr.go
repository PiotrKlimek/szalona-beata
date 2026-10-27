package main

import (
	"bytes"
	"context"
	"fmt"
	"image"
	_ "image/jpeg" // WhatsApp sends photos as JPEG
	_ "image/png"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode"
)

const (
	ocrTag      = "[text in image]" // marks recognized text in a stored message; the summarizer's prompt refers to it
	ocrMinConf  = 60                // words Tesseract is less sure about are mostly noise
	ocrMinWords = 3                 // fewer confident words of 3+ letters/digits: no text worth summarizing
	ocrMaxChars = 1500              // keeps one image from eating the summarizer's transcript budget
	ocrTimeout  = 2 * time.Minute
)

// ocrResult is what happened to one image, for logs and the ocr subcommand.
type ocrResult struct {
	Chars   int    // letters in rows found by the fast check
	Skipped bool   // the fast check found no text, Tesseract didn't run
	Text    string // recognized text, "" if none
}

// recognize runs the fast text check and, if it finds text, Tesseract.
func recognize(ctx context.Context, data []byte, langs string) (ocrResult, error) {
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return ocrResult{}, fmt.Errorf("decode image: %w", err)
	}
	chars, ok := detectText(img)
	res := ocrResult{Chars: chars, Skipped: !ok}
	if !ok {
		return res, nil
	}
	tsv, err := tesseract(ctx, data, langs)
	if err != nil {
		return res, err
	}
	res.Text = parseTesseractTSV(tsv)
	return res, nil
}

// tesseract runs the tesseract CLI on an image and returns its TSV output.
func tesseract(ctx context.Context, data []byte, langs string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, ocrTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "tesseract", "stdin", "stdout", "-l", langs, "--psm", "3", "tsv")
	cmd.Stdin = bytes.NewReader(data)
	// One thread: Tesseract's OpenMP threads are slower on small servers and would starve the bridge.
	cmd.Env = append(os.Environ(), "OMP_THREAD_LIMIT=1")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("tesseract: %w: %s", err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

// parseTesseractTSV rebuilds the text from Tesseract's TSV output, one output
// line per recognized line, keeping only confident words that contain a letter
// or digit. It returns "" when too few real words are left to be text.
func parseTesseractTSV(tsv string) string {
	var lines []string
	var line []string
	lineKey, words := "", 0
	flush := func() {
		if len(line) > 0 {
			lines = append(lines, strings.Join(line, " "))
			line = nil
		}
	}
	for i, row := range strings.Split(tsv, "\n") {
		// level page_num block_num par_num line_num word_num left top width height conf text
		f := strings.Split(strings.TrimRight(row, "\r"), "\t")
		if i == 0 || len(f) < 12 || f[0] != "5" {
			continue
		}
		conf, err := strconv.ParseFloat(f[10], 64)
		word := strings.TrimSpace(f[11])
		if err != nil || conf < ocrMinConf || !strings.ContainsFunc(word, isAlnum) {
			continue
		}
		if key := f[2] + "." + f[3] + "." + f[4]; key != lineKey {
			flush()
			lineKey = key
		}
		line = append(line, word)
		if countFunc(word, isAlnum) >= 3 { // OCR noise is mostly 1-2 character "words"
			words++
		}
	}
	flush()
	if words < ocrMinWords {
		return ""
	}
	return truncateRunes(strings.Join(lines, "\n"), ocrMaxChars)
}

func isAlnum(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) }

func countFunc(s string, f func(rune) bool) int {
	n := 0
	for _, r := range s {
		if f(r) {
			n++
		}
	}
	return n
}

func truncateRunes(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n-1]) + "…"
	}
	return s
}

// ocrSuffix is what gets appended to an image message's text for recognized text.
func ocrSuffix(ocr string) string {
	if ocr == "" {
		return ""
	}
	return "\n" + ocrTag + "\n" + ocr
}

// runOCRCommand implements "bridge ocr FILE|DIR...": shows what the fast check
// and Tesseract make of local images, to see how the detection does on your photos.
func runOCRCommand(paths []string, langs string) error {
	var files []string
	for _, p := range paths {
		entries, err := os.ReadDir(p)
		if err != nil {
			files = append(files, p) // not a directory
			continue
		}
		for _, e := range entries {
			if !e.IsDir() {
				files = append(files, filepath.Join(p, e.Name()))
			}
		}
	}
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			return err
		}
		start := time.Now()
		res, err := recognize(context.Background(), data, langs)
		took := time.Since(start).Round(time.Millisecond)
		switch {
		case err != nil:
			fmt.Printf("%s: error: %v\n", f, err)
		case res.Skipped:
			fmt.Printf("%s: no text (%d letters in rows), %s\n", f, res.Chars, took)
		case res.Text == "":
			fmt.Printf("%s: checked by OCR, nothing confident (%d letters in rows), %s\n", f, res.Chars, took)
		default:
			fmt.Printf("%s: text (%d letters in rows), %s\n  %s\n", f, res.Chars, took, strings.ReplaceAll(res.Text, "\n", "\n  "))
		}
	}
	return nil
}

// checkTesseract verifies that tesseract is installed with every language in langs ("pol+eng").
func checkTesseract(langs string) error {
	out, err := exec.Command("tesseract", "--list-langs").Output()
	if err != nil {
		return fmt.Errorf("tesseract not available: %w", err)
	}
	installed := map[string]bool{}
	for _, l := range strings.Fields(string(out)) {
		installed[l] = true
	}
	for _, l := range strings.Split(langs, "+") {
		if !installed[l] {
			return fmt.Errorf("tesseract language %q from OCR_LANGS is not installed", l)
		}
	}
	return nil
}

// ocrLangs returns the Tesseract languages from OCR_LANGS, "" when OCR is off.
func ocrLangs() string {
	v := strings.TrimSpace(envOr("OCR_LANGS", "pol+eng"))
	if strings.EqualFold(v, "off") {
		return ""
	}
	return v
}
