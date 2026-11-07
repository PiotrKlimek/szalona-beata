package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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
	pdfMinLetters = 20   // a text layer with fewer letters/digits is a scan (or empty): try OCR
	pdfMaxChars   = 3000 // keeps one document from eating the summarizer's transcript budget
	pdfTimeout    = 5 * time.Minute
)

// pdfResult is what was read from one PDF, for logs and the ocr subcommand.
type pdfResult struct {
	Text  string // "" if none
	OCR   bool   // the text was read from rendered pages (a scan), not the text layer
	Pages int    // pages read by OCR
}

// isReadablePDF reports whether a document message is a PDF small enough to read.
func isReadablePDF(d *waE2E.DocumentMessage) bool {
	pdf := strings.EqualFold(d.GetMimetype(), "application/pdf") || strings.HasSuffix(strings.ToLower(d.GetFileName()), ".pdf")
	return pdf && d.GetFileLength() <= pdfMaxBytes
}

// readPDF returns the text of a PDF: its text layer, or, when that is (nearly) empty
// and ocrLangs isn't "", the first pages rendered and read by Tesseract.
func readPDF(ctx context.Context, data []byte, ocrLangs string) (pdfResult, error) {
	if !bytes.HasPrefix(data, []byte("%PDF")) {
		return pdfResult{}, errors.New("not a PDF")
	}
	ctx, cancel := context.WithTimeout(ctx, pdfTimeout)
	defer cancel()
	dir, err := os.MkdirTemp("", "pdf")
	if err != nil {
		return pdfResult{}, err
	}
	defer os.RemoveAll(dir)
	file := filepath.Join(dir, "doc.pdf")
	if err := os.WriteFile(file, data, 0o600); err != nil {
		return pdfResult{}, err
	}

	out, err := runTool(ctx, "pdftotext", "-l", strconv.Itoa(pdfTextPages), "-enc", "UTF-8", file, "-")
	if err != nil {
		return pdfResult{}, err
	}
	if text := cleanPDFText(out); countFunc(text, isAlnum) >= pdfMinLetters {
		return pdfResult{Text: truncateRunes(text, pdfMaxChars)}, nil
	}
	if ocrLangs == "" {
		return pdfResult{}, nil
	}

	prefix := filepath.Join(dir, "page")
	if _, err := runTool(ctx, "pdftoppm", "-r", strconv.Itoa(pdfOCRDPI), "-gray", "-png",
		"-l", strconv.Itoa(pdfOCRPages), file, prefix); err != nil {
		return pdfResult{}, err
	}
	pages, _ := filepath.Glob(prefix + "*.png")
	sort.Strings(pages) // pdftoppm pads page numbers to the same width
	res := pdfResult{OCR: true, Pages: len(pages)}
	var texts []string
	for _, p := range pages {
		png, err := os.ReadFile(p)
		if err != nil {
			return res, err
		}
		tsv, err := tesseract(ctx, png, ocrLangs)
		if err != nil {
			return res, err
		}
		if t := parseTesseractTSV(tsv); t != "" {
			texts = append(texts, t)
		}
	}
	res.Text = truncateRunes(strings.Join(texts, "\n"), pdfMaxChars)
	return res, nil
}

// cleanPDFText trims pdftotext output: no page breaks, empty lines or runs of spaces.
func cleanPDFText(s string) string {
	var lines []string
	for _, l := range strings.Split(strings.ReplaceAll(s, "\f", "\n"), "\n") {
		if l = strings.Join(strings.Fields(l), " "); l != "" {
			lines = append(lines, l)
		}
	}
	return strings.Join(lines, "\n")
}

// runTool runs a command and returns its stdout; stderr goes into the error.
func runTool(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("%s: %w: %s", name, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
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
	for _, tool := range []string{"pdftotext", "pdftoppm"} {
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
