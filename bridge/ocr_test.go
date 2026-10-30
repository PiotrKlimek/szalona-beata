package main

import (
	"bytes"
	"context"
	"image"
	"image/color"
	"image/png"
	"strings"
	"testing"
)

const tsvHeader = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"

func tsvWord(block, par, line int, conf, text string) string {
	return strings.Join([]string{"5", "1", itoa(block), itoa(par), itoa(line), "1", "0", "0", "10", "10", conf, text}, "\t") + "\n"
}

func itoa(i int) string { return string(rune('0' + i)) }

func TestParseTesseractTSVRebuildsLines(t *testing.T) {
	tsv := tsvHeader +
		"1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n" + // page row
		tsvWord(1, 1, 1, "96.1", "Zebranie") +
		tsvWord(1, 1, 1, "91.0", "rodziców") +
		tsvWord(1, 1, 1, "30.2", "~|") + // unsure noise
		tsvWord(1, 1, 2, "88.5", "wtorek") +
		tsvWord(1, 1, 2, "90", "18:00") +
		tsvWord(2, 1, 1, "85", "—") + // no letters or digits
		tsvWord(2, 1, 1, "85", "sala") +
		tsvWord(2, 1, 1, "85", "12")

	got := parseTesseractTSV(tsv)

	want := "Zebranie rodziców\nwtorek 18:00\nsala 12"
	if got != want {
		t.Errorf("parseTesseractTSV() = %q; want %q", got, want)
	}
}

func TestParseTesseractTSVDropsNoise(t *testing.T) {
	tests := map[string]string{
		"empty":           tsvHeader,
		"unsure words":    tsvHeader + tsvWord(1, 1, 1, "40", "Zebranie") + tsvWord(1, 1, 1, "41", "rodziców") + tsvWord(1, 1, 1, "42", "wtorek"),
		"short fragments": tsvHeader + tsvWord(1, 1, 1, "90", "e") + tsvWord(1, 1, 1, "90", "QYTE") + tsvWord(1, 1, 2, "90", "IAS") + tsvWord(1, 1, 2, "90", "4") + tsvWord(1, 1, 2, "90", "in"),
		"not tsv":         "tesseract: error",
	}
	for name, tsv := range tests {
		if got := parseTesseractTSV(tsv); got != "" {
			t.Errorf("%s: parseTesseractTSV() = %q; want \"\"", name, got)
		}
	}
}

func TestParseTesseractTSVCapsLength(t *testing.T) {
	tsv := tsvHeader
	for range 1000 {
		tsv += tsvWord(1, 1, 1, "90", "słowo")
	}

	got := []rune(parseTesseractTSV(tsv))

	if len(got) != ocrMaxChars || got[len(got)-1] != '…' {
		t.Errorf("got %d runes ending in %q; want %d ending in …", len(got), got[len(got)-1], ocrMaxChars)
	}
}

// Runs only where tesseract is installed (the bridge image, not its Go build stage).
func TestRecognizeWithTesseract(t *testing.T) {
	if err := checkTesseract("pol+eng"); err != nil {
		t.Skip(err)
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, textImage(page, 3, color.Black, color.White)); err != nil {
		t.Fatal(err)
	}

	res, err := recognize(context.Background(), buf.Bytes(), "pol+eng")

	if err != nil || res.Skipped || !strings.Contains(res.Text, "Centrum Nauki") || !strings.Contains(res.Text, "Koszt 65") {
		t.Errorf("recognize() = %+v, %v; want the page text", res, err)
	}
}

func TestRecognizeSkipsOCRWithoutText(t *testing.T) {
	var buf bytes.Buffer
	png.Encode(&buf, image.NewGray(image.Rect(0, 0, 800, 600)))

	// No tesseract needed: an image without text never reaches it.
	res, err := recognize(context.Background(), buf.Bytes(), "no-such-language")

	if err != nil || !res.Skipped || res.Text != "" {
		t.Errorf("recognize() = %+v, %v; want skipped", res, err)
	}
}

func TestRecognizeRejectsNonImages(t *testing.T) {
	if _, err := recognize(context.Background(), []byte("not an image"), "pol+eng"); err == nil {
		t.Error("recognize(garbage) succeeded; want a decode error")
	}
}
