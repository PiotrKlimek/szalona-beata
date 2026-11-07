package main

import (
	"bytes"
	"context"
	"fmt"
	"image/color"
	"image/jpeg"
	"os/exec"
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
		{"pdf by name", &waE2E.DocumentMessage{FileName: proto.String("Zadanie.PDF")}, true},
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
	_, err1 := exec.LookPath("pdftotext")
	_, err2 := exec.LookPath("pdftoppm")
	if (err1 == nil && err2 == nil) != (checkPoppler() == nil) {
		t.Errorf("checkPoppler() = %v; tools found: pdftotext %v, pdftoppm %v", checkPoppler(), err1 == nil, err2 == nil)
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
