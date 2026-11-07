package main

import (
	"bytes"
	"context"
	"fmt"
	"image/color"
	"image/jpeg"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// pdfObject is one object of a test PDF: a dictionary and an optional stream.
type pdfObject struct {
	dict   string
	stream []byte
}

// buildPDF writes a minimal PDF (objects numbered from 1, object 1 the catalog) with a valid xref table.
func buildPDF(objects ...pdfObject) []byte {
	var b bytes.Buffer
	b.WriteString("%PDF-1.4\n")
	offsets := make([]int, len(objects))
	for i, o := range objects {
		offsets[i] = b.Len()
		if o.stream == nil {
			fmt.Fprintf(&b, "%d 0 obj\n%s\nendobj\n", i+1, o.dict)
			continue
		}
		dict := strings.TrimSuffix(o.dict, ">>") + fmt.Sprintf(" /Length %d >>", len(o.stream))
		fmt.Fprintf(&b, "%d 0 obj\n%s\nstream\n", i+1, dict)
		b.Write(o.stream)
		b.WriteString("\nendstream\nendobj\n")
	}
	xref := b.Len()
	fmt.Fprintf(&b, "xref\n0 %d\n0000000000 65535 f \n", len(objects)+1)
	for _, off := range offsets {
		fmt.Fprintf(&b, "%010d 00000 n \n", off)
	}
	fmt.Fprintf(&b, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(objects)+1, xref)
	return b.Bytes()
}

// textPDF is a one-page PDF with a text layer.
func textPDF(lines ...string) []byte {
	var content strings.Builder
	content.WriteString("BT /F1 14 Tf 50 780 Td")
	for _, l := range lines {
		fmt.Fprintf(&content, " (%s) Tj 0 -20 Td", l)
	}
	content.WriteString(" ET")
	return buildPDF(
		pdfObject{dict: "<< /Type /Catalog /Pages 2 0 R >>"},
		pdfObject{dict: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"},
		pdfObject{dict: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] " +
			"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"},
		pdfObject{dict: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"},
		pdfObject{dict: "<< >>", stream: []byte(content.String())},
	)
}

// scanPDF is a one-page PDF holding only a JPEG of the text (like a phone scanner app makes).
func scanPDF(t *testing.T, lines []string) []byte {
	t.Helper()
	img := textImage(lines, 3, color.Black, color.White)
	var jpg bytes.Buffer
	if err := jpeg.Encode(&jpg, img, &jpeg.Options{Quality: 90}); err != nil {
		t.Fatal(err)
	}
	w, h := img.Bounds().Dx(), img.Bounds().Dy()
	pw, ph := w*72/150, h*72/150 // scanned at 150 dpi
	return buildPDF(
		pdfObject{dict: "<< /Type /Catalog /Pages 2 0 R >>"},
		pdfObject{dict: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"},
		pdfObject{dict: fmt.Sprintf("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "+
			"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>", pw, ph)},
		pdfObject{dict: fmt.Sprintf("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "+
			"/BitsPerComponent 8 /Filter /DCTDecode >>", w, h), stream: jpg.Bytes()},
		pdfObject{dict: "<< >>", stream: []byte(fmt.Sprintf("q %d 0 0 %d 0 0 cm /Im0 Do Q", pw, ph))},
	)
}

// Runs only where poppler-utils are installed (the bridge image and its build stage).
func needPoppler(t *testing.T) {
	t.Helper()
	if err := checkPoppler(); err != nil {
		t.Skip(err)
	}
}

func TestReadPDFTextLayer(t *testing.T) {
	needPoppler(t)
	data := textPDF("Zadanie domowe na piatek:", "podrecznik str. 45, cw. 3 i 4")

	res, err := readPDF(context.Background(), data, "")

	if err != nil || res.OCR || res.Text != "Zadanie domowe na piatek:\npodrecznik str. 45, cw. 3 i 4" {
		t.Errorf("readPDF() = %+v, %v; want the text layer, no OCR", res, err)
	}
}

func TestReadPDFScanWithOCR(t *testing.T) {
	needPoppler(t)
	if err := checkTesseract("pol+eng"); err != nil {
		t.Skip(err)
	}

	res, err := readPDF(context.Background(), scanPDF(t, page), "pol+eng")

	if err != nil || !res.OCR || res.Pages != 1 || !strings.Contains(res.Text, "Centrum Nauki") || !strings.Contains(res.Text, "Koszt 65") {
		t.Errorf("readPDF() = %+v, %v; want the page text read by OCR", res, err)
	}
}

func TestReadPDFScanWithoutOCRHasNoText(t *testing.T) {
	needPoppler(t)

	res, err := readPDF(context.Background(), scanPDF(t, page), "")

	if err != nil || res.OCR || res.Text != "" {
		t.Errorf("readPDF() = %+v, %v; want no text (OCR off)", res, err)
	}
}

func TestReadPDFTruncatesLongText(t *testing.T) {
	needPoppler(t)
	lines := make([]string, 40)
	for i := range lines {
		lines[i] = strings.Repeat("Bardzo dlugi tekst ", 6)
	}
	// One page fits ~38 lines of 14 pt; enough characters either way.
	res, err := readPDF(context.Background(), textPDF(lines...), "")

	if got := []rune(res.Text); err != nil || len(got) != pdfMaxChars || got[len(got)-1] != '…' {
		t.Errorf("readPDF() = %d runes, %v; want %d ending in …", len(got), err, pdfMaxChars)
	}
}

func TestReadPDFScannerWatermarkIsNotText(t *testing.T) {
	needPoppler(t)
	// A phone scanner app's footer is the only text layer of a scan: OCR must still run (off here: no text).
	res, err := readPDF(context.Background(), textPDF("Scanned with CamScanner"), "")
	if err != nil || res.Text != "" {
		t.Errorf("readPDF() = %+v, %v; want no text", res, err)
	}
}

func TestReadPDFHugePageIsRenderedSmall(t *testing.T) {
	needPoppler(t)
	if err := checkTesseract("pol+eng"); err != nil {
		t.Skip(err)
	}
	// 200 inches square: at 200 dpi that would be 40000x40000 pixels (GBs of memory).
	data := buildPDF(
		pdfObject{dict: "<< /Type /Catalog /Pages 2 0 R >>"},
		pdfObject{dict: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"},
		pdfObject{dict: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 14400 14400] >>"},
	)
	start := time.Now()
	res, err := readPDF(context.Background(), data, "pol+eng")
	if err != nil || !res.OCR || res.Text != "" || time.Since(start) > 30*time.Second {
		t.Errorf("readPDF() = %+v, %v in %s; want a quick empty OCR result", res, err, time.Since(start))
	}
}

func TestReadPDFShortTextLayerBeatsOCR(t *testing.T) {
	needPoppler(t)
	if err := checkTesseract("pol+eng"); err != nil {
		t.Skip(err)
	}
	// Short for one page, so OCR runs, but OCR can only misread what the text layer has exactly.
	res, err := readPDF(context.Background(), textPDF("Zadanie domowe na piatek:", "podrecznik str. 45, cw. 3 i 4"), "pol+eng")
	if err != nil || res.OCR || res.Text != "Zadanie domowe na piatek:\npodrecznik str. 45, cw. 3 i 4" {
		t.Errorf("readPDF() = %+v, %v; want the exact text layer", res, err)
	}
}

func TestReadPDFScannerWatermarkReadByOCRIsDropped(t *testing.T) {
	needPoppler(t)
	if err := checkTesseract("pol+eng"); err != nil {
		t.Skip(err)
	}
	res, err := readPDF(context.Background(), scanPDF(t, []string{"Scanned with CamScanner"}), "pol+eng")
	if err != nil || res.Text != "" {
		t.Errorf("readPDF() = %+v, %v; want no text", res, err)
	}
}

func TestLooksLikePDF(t *testing.T) {
	for in, want := range map[string]bool{
		"%PDF-1.4\n":                           true,
		strings.Repeat("x", 500) + "%PDF-1.7":  true, // junk before the header is allowed
		strings.Repeat("x", 1100) + "%PDF-1.7": false,
		"<html>":                               false,
	} {
		if got := looksLikePDF([]byte(in)); got != want {
			t.Errorf("looksLikePDF(%.20q...) = %v; want %v", in, got, want)
		}
	}
}

func TestRunToolCapsOutput(t *testing.T) {
	out, err := runTool(context.Background(), "sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' a")
	if err != nil || len(out) != pdfMaxToolOutput {
		t.Errorf("runTool() = %d bytes, %v; want %d", len(out), err, pdfMaxToolOutput)
	}
	_, err = runTool(context.Background(), "sh", "-c", "head -c 100000 /dev/zero | tr '\\0' e >&2; exit 1")
	if err == nil || len(err.Error()) > 600 {
		t.Errorf("runTool() error = %d bytes; want a short error", len(fmt.Sprint(err)))
	}
}

func TestRemoveStalePDFDirs(t *testing.T) {
	base := t.TempDir()
	os.Mkdir(filepath.Join(base, pdfTempPrefix+"123"), 0o700)
	os.Mkdir(filepath.Join(base, "other"), 0o700)

	removeStalePDFDirs(base)

	if entries, _ := os.ReadDir(base); len(entries) != 1 || entries[0].Name() != "other" {
		t.Errorf("left %v; want only other", entries)
	}
}

func TestReadPDFRejectsNonPDFs(t *testing.T) {
	// No poppler needed: checked before anything runs.
	if _, err := readPDF(context.Background(), []byte("<html>not a pdf</html>"), ""); err == nil {
		t.Error("readPDF(html) succeeded; want an error")
	}
}

func TestCleanPDFText(t *testing.T) {
	got := cleanPDFText("  Zadanie   domowe \n\n\f str. 45\t cw. 3 \n\f")
	if got != "Zadanie domowe\nstr. 45 cw. 3" {
		t.Errorf("cleanPDFText() = %q", got)
	}
}

func TestQueuesPDFDocumentsOnly(t *testing.T) {
	tests := []struct {
		name string
		doc  *waE2E.DocumentMessage
		want bool
	}{
		{"pdf by mimetype", &waE2E.DocumentMessage{Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(200_000)}, true},
		{"pdf by name", &waE2E.DocumentMessage{FileName: proto.String("Zadanie.PDF"), FileLength: proto.Uint64(1000)}, true},
		{"size unknown", &waE2E.DocumentMessage{Mimetype: proto.String("application/pdf")}, false},
		{"word document", &waE2E.DocumentMessage{Mimetype: proto.String("application/msword"), FileName: proto.String("a.doc")}, false},
		{"too big", &waE2E.DocumentMessage{Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(pdfMaxBytes + 1)}, false},
	}
	for _, tt := range tests {
		if got := isReadablePDF(tt.doc); got != tt.want {
			t.Errorf("%s: isReadablePDF() = %v; want %v", tt.name, got, tt.want)
		}
	}
}

func TestPDFWorkerAppendsDocumentText(t *testing.T) {
	s := newTestStore(t)
	msg := Message{ChatJID: "120363000000000001@g.us", ID: "DOC1", Timestamp: time.Unix(1761200000, 0),
		Text: "[document] Zadanie.pdf"}
	if _, err := s.SaveMessage(msg); err != nil {
		t.Fatal(err)
	}
	download := func(context.Context, mediaMessage) ([]byte, error) { return []byte("%PDF-1.4"), nil }
	extract := func(context.Context, []byte) (string, error) { return "Str. 45, cw. 3 i 4", nil }
	newDoc := func() mediaMessage { return &waE2E.DocumentMessage{} }
	w := newMediaWorker(s, pdfQueue, "PDF", newDoc, download, extract, waLog.Noop)
	w.Enqueue(msg.ChatJID, msg.ID, msg.Timestamp, &waE2E.DocumentMessage{FileName: proto.String("Zadanie.pdf")})

	if !w.processNext(context.Background()) {
		t.Fatal("processNext() = false; want true (a PDF was queued)")
	}
	if got := messageText(t, s, "DOC1"); got != "[document] Zadanie.pdf\n[text in document]\nStr. 45, cw. 3 i 4" {
		t.Errorf("message text = %q", got)
	}
}

func TestPopplerToolsAreChecked(t *testing.T) {
	all := true
	for _, tool := range []string{"pdftotext", "pdftoppm", "pdfinfo"} {
		if _, err := exec.LookPath(tool); err != nil {
			all = false
		}
	}
	if all != (checkPoppler() == nil) {
		t.Errorf("checkPoppler() = %v; all tools found: %v", checkPoppler(), all)
	}
}

func TestPDFEnabled(t *testing.T) {
	for v, want := range map[string]bool{"": true, "on": true, " OFF ": false} {
		t.Setenv("PDF_TEXT", v)
		if got, err := pdfEnabled(); err != nil || got != want {
			t.Errorf("PDF_TEXT=%q: pdfEnabled() = %v, %v; want %v", v, got, err, want)
		}
	}
	t.Setenv("PDF_TEXT", "yes")
	if _, err := pdfEnabled(); err == nil {
		t.Error("PDF_TEXT=yes accepted; want an error")
	}
}

func TestPDFRenderDPI(t *testing.T) {
	needPoppler(t)
	dir := t.TempDir()
	for size, want := range map[string]int{"595 842": 200, "14400 14400": 12} {
		file := filepath.Join(dir, "p.pdf")
		os.WriteFile(file, buildPDF(
			pdfObject{dict: "<< /Type /Catalog /Pages 2 0 R >>"},
			pdfObject{dict: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"},
			pdfObject{dict: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + size + "] >>"},
		), 0o600)
		if got, err := pdfRenderDPI(context.Background(), file); err != nil || got != want {
			t.Errorf("page %s pt: pdfRenderDPI() = %d, %v; want %d", size, got, err, want)
		}
	}
}
