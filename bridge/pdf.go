package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	waLog "go.mau.fi/whatsmeow/util/log"
)

const (
	pdfTag        = "[text in document]" // marks text read from a PDF in a stored message; the summarizer's prompt refers to it
	pdfMaxBytes   = 20 << 20             // bigger files are rarely a notice or homework, and slow to read
	pdfTextPages  = 10                   // pages read from the text layer
	pdfOCRPages   = 3                    // pages of a scan read by OCR (~10-30 s each on a small server)
	pdfOCRDPI     = 200
	pdfOCRMaxPx   = 2500 // long side of a rendered page: an A4 page at 200 dpi, while a huge page can't take GBs
	pdfMinLetters = 50   // per page: a text layer with fewer letters/digits may be a scan: try OCR
	pdfMinText    = 20   // letters/digits that make a text layer worth keeping when OCR can't do better
	pdfMaxChars   = 3000 // keeps one document from eating the summarizer's transcript budget
	pdfTimeout    = 5 * time.Minute

	pdfMaxToolOutput = 1 << 20 // only pdfMaxChars are kept: a crafted PDF can't fill memory through pdftotext
	pdfMaxToolErr    = 300     // of a tool's stderr in errors and logs (corrupt PDFs print thousands of lines)
	pdfToolMemory    = 1 << 30 // address space limit of poppler (prlimit), so one PDF can't exhaust the server
	pdfTempPrefix    = "szb-pdf-"
)

// Footers that phone scanner apps put into the text layer of a scan: not the document's text.
var scannerWatermark = regexp.MustCompile(`(?i)^(scanned (with|by)|created (with|by)|zeskanowano)\b|camscanner|adobe scan|genius scan|microsoft lens|tinyscanner`)

// pdfResult is what was read from one PDF, for logs and the ocr subcommand.
type pdfResult struct {
	Text  string // "" if none
	OCR   bool   // the text was read from rendered pages (a scan), not the text layer
	Pages int    // pages read by OCR
}

// isReadablePDF reports whether a document message is a PDF small enough to read.
func isReadablePDF(d *waE2E.DocumentMessage) bool {
	pdf := strings.EqualFold(d.GetMimetype(), "application/pdf") || strings.HasSuffix(strings.ToLower(d.GetFileName()), ".pdf")
	// The size is declared by the sender: unknown (0) is not trusted, the download is read into memory.
	return pdf && d.GetFileLength() > 0 && d.GetFileLength() <= pdfMaxBytes
}

// looksLikePDF reports whether data has a PDF header in its first 1024 bytes (junk before it is allowed).
func looksLikePDF(data []byte) bool {
	return bytes.Contains(data[:min(len(data), 1024)], []byte("%PDF-"))
}

// readPDF returns the text of a PDF: its text layer, or, when that is short for the number
// of pages (a scan) and ocrLangs isn't "", the first pages read by Tesseract if that finds more.
func readPDF(ctx context.Context, data []byte, ocrLangs string) (pdfResult, error) {
	if !looksLikePDF(data) {
		return pdfResult{}, errors.New("not a PDF")
	}
	ctx, cancel := context.WithTimeout(ctx, pdfTimeout)
	defer cancel()
	dir, err := os.MkdirTemp("", pdfTempPrefix)
	if err != nil {
		return pdfResult{}, err
	}
	defer os.RemoveAll(dir)
	file := filepath.Join(dir, "doc.pdf")
	if err := os.WriteFile(file, data, 0o600); err != nil {
		return pdfResult{}, err
	}

	out, err := runTool(ctx, "pdftotext", "-q", "-l", strconv.Itoa(pdfTextPages), "-enc", "UTF-8", file, "-")
	if err != nil {
		return pdfResult{}, err
	}
	pages := max(1, strings.Count(out, "\f")) // pdftotext ends every page with a form feed
	layer := cleanPDFText(out)
	letters := countFunc(layer, isAlnum)
	if letters < pdfMinText {
		layer, letters = "", 0
	}
	textLayer := pdfResult{Text: truncateRunes(layer, pdfMaxChars)}
	if letters >= pdfMinLetters*pages || ocrLangs == "" {
		return textLayer, nil
	}
	// Little text for the number of pages: maybe a scan with a few words of text layer. The text
	// layer is exact, so OCR wins only when it finds clearly more (at least twice as many letters).
	res, err := ocrPDF(ctx, dir, file, ocrLangs)
	if err == nil && letters > 0 && countFunc(res.Text, isAlnum) < 2*letters {
		return textLayer, nil
	}
	return res, err
}

// ocrPDF renders the first pages of a PDF and reads them with Tesseract.
func ocrPDF(ctx context.Context, dir, file, ocrLangs string) (pdfResult, error) {
	dpi, err := pdfRenderDPI(ctx, file)
	if err != nil {
		return pdfResult{}, err
	}
	prefix := filepath.Join(dir, "page")
	if _, err := runTool(ctx, "pdftoppm", "-q", "-r", strconv.Itoa(dpi), "-gray", "-png",
		"-l", strconv.Itoa(pdfOCRPages), file, prefix); err != nil {
		return pdfResult{}, err
	}
	images, _ := filepath.Glob(prefix + "*.png")
	sort.Strings(images) // pdftoppm pads page numbers to the same width
	res := pdfResult{OCR: true, Pages: len(images)}
	var texts []string
	for _, p := range images {
		png, err := os.ReadFile(p)
		if err != nil {
			return res, err
		}
		tsv, err := tesseract(ctx, png, ocrLangs)
		if err != nil {
			return res, err
		}
		if t := cleanPDFText(parseTesseractTSV(tsv)); t != "" { // scanner app footers are in the image too
			texts = append(texts, t)
		}
	}
	res.Text = truncateRunes(strings.Join(texts, "\n"), pdfMaxChars)
	return res, nil
}

var pdfPageSize = regexp.MustCompile(`size:\s*([0-9.]+) x ([0-9.]+) pts`)

// pdfRenderDPI is pdfOCRDPI, lowered so the largest of the pages to OCR is at most pdfOCRMaxPx
// pixels on its long side: a crafted page size must not take gigabytes to render.
func pdfRenderDPI(ctx context.Context, file string) (int, error) {
	out, err := runTool(ctx, "pdfinfo", "-f", "1", "-l", strconv.Itoa(pdfOCRPages), file)
	if err != nil {
		return 0, err
	}
	longest := 0.0
	for _, m := range pdfPageSize.FindAllStringSubmatch(out, -1) {
		for _, v := range m[1:] {
			if pt, err := strconv.ParseFloat(v, 64); err == nil {
				longest = max(longest, pt)
			}
		}
	}
	if longest <= 0 {
		return 0, errors.New("pdfinfo: no page size")
	}
	return max(1, min(pdfOCRDPI, int(pdfOCRMaxPx*72/longest))), nil
}

// cleanPDFText trims pdftotext output: no page breaks, empty lines, runs of spaces or scanner app footers.
func cleanPDFText(s string) string {
	var lines []string
	for _, l := range strings.Split(strings.ReplaceAll(s, "\f", "\n"), "\n") {
		if l = strings.Join(strings.Fields(l), " "); l != "" && !scannerWatermark.MatchString(l) {
			lines = append(lines, l)
		}
	}
	return strings.Join(lines, "\n")
}

// cappedBuffer keeps the first max bytes written to it and drops the rest (the tool keeps running).
// Not an embedded bytes.Buffer: its ReadFrom would let io.Copy bypass the cap.
type cappedBuffer struct {
	buf bytes.Buffer
	max int
}

func (b *cappedBuffer) Write(p []byte) (int, error) {
	if room := b.max - b.buf.Len(); room > 0 {
		b.buf.Write(p[:min(len(p), room)])
	}
	return len(p), nil
}

func (b *cappedBuffer) String() string { return b.buf.String() }

// runTool runs a command (with a memory limit when prlimit is there) and returns its stdout,
// capped at pdfMaxToolOutput; the start of stderr goes into the error.
func runTool(ctx context.Context, name string, args ...string) (string, error) {
	tool := name
	if prlimit, err := exec.LookPath("prlimit"); err == nil {
		args = append([]string{"--as=" + strconv.Itoa(pdfToolMemory), "--", name}, args...)
		name = prlimit
	}
	cmd := exec.CommandContext(ctx, name, args...)
	stdout, stderr := &cappedBuffer{max: pdfMaxToolOutput}, &cappedBuffer{max: pdfMaxToolErr}
	cmd.Stdout, cmd.Stderr = stdout, stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s: %w: %s", tool, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

// removeStalePDFDirs deletes temporary folders left by readPDF when the bridge was stopped mid-PDF.
func removeStalePDFDirs(base string) {
	dirs, _ := filepath.Glob(filepath.Join(base, pdfTempPrefix+"*"))
	for _, d := range dirs {
		os.RemoveAll(d)
	}
}

// pdfText returns the mediaWorker extract function for PDF documents.
func pdfText(ocrLangs string, log waLog.Logger) func(context.Context, []byte) (string, error) {
	return func(ctx context.Context, data []byte) (string, error) {
		res, err := readPDF(ctx, data, ocrLangs)
		if err == nil && res.OCR {
			log.Debugf("PDF without a text layer: read %d pages by OCR", res.Pages)
		}
		return res.Text, err
	}
}

// checkPoppler verifies that the poppler tools used for PDFs are installed.
func checkPoppler() error {
	for _, tool := range []string{"pdftotext", "pdftoppm", "pdfinfo"} {
		if _, err := exec.LookPath(tool); err != nil {
			return fmt.Errorf("%s not available (poppler-utils): %w", tool, err)
		}
	}
	return nil
}

// pdfEnabled reports whether PDF_TEXT allows reading PDFs (on by default: nothing leaves the server).
func pdfEnabled() (bool, error) {
	switch v := strings.ToLower(strings.TrimSpace(os.Getenv("PDF_TEXT"))); v {
	case "", "on":
		return true, nil
	case "off":
		return false, nil
	default:
		return false, fmt.Errorf("PDF_TEXT must be on or off, got %q", v)
	}
}
