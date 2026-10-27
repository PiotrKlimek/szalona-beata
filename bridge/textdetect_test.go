package main

import (
	"image"
	"image/color"
	"image/draw"
	"math/rand"
	"testing"

	"golang.org/x/image/font"
	"golang.org/x/image/font/basicfont"
	"golang.org/x/image/math/fixed"
)

// textImage renders lines of text (fg on bg) and scales it up, like a photo of a page.
func textImage(lines []string, scale int, fg, bg color.Color) image.Image {
	small := image.NewRGBA(image.Rect(0, 0, 420, 20+16*len(lines)))
	draw.Draw(small, small.Bounds(), image.NewUniform(bg), image.Point{}, draw.Src)
	d := font.Drawer{Dst: small, Src: image.NewUniform(fg), Face: basicfont.Face7x13}
	for i, l := range lines {
		d.Dot = fixed.P(10, 20+16*i)
		d.DrawString(l)
	}
	big := image.NewRGBA(image.Rect(0, 0, small.Bounds().Dx()*scale, small.Bounds().Dy()*scale))
	for y := range big.Bounds().Dy() {
		for x := range big.Bounds().Dx() {
			big.Set(x, y, small.At(x/scale, y/scale))
		}
	}
	return big
}

var page = []string{
	"Drodzy Rodzice,",
	"wycieczka do Centrum Nauki w czwartek 6.11.",
	"Zbiorka 8:00 pod szkola, powrot ok. 14:30.",
	"Koszt 65 zl, wplata do poniedzialku.",
}

func TestHasTextFindsPrintedText(t *testing.T) {
	tests := map[string]image.Image{
		"dark on light":     textImage(page, 3, color.Black, color.White),
		"light on dark":     textImage(page, 3, color.White, color.RGBA{20, 40, 120, 255}),
		"small photo":       textImage(page, 2, color.Black, color.White),
		"big letters":       textImage(page[:2], 8, color.Black, color.White),
		"low contrast grey": textImage(page, 3, color.Gray{70}, color.Gray{170}),
	}
	for name, img := range tests {
		if !hasText(img) {
			t.Errorf("%s: hasText = false (%d letters in rows); want true", name, textChars(img))
		}
	}
}

func TestHasTextFindsSmallPrintInBigImages(t *testing.T) {
	// A phone screenshot of a long text: 13 px letters on 1150x2050, which the
	// analysis size shrinks to 4 px.
	var lines []string
	for range 25 {
		lines = append(lines, page...)
	}
	text := textImage(lines, 1, color.Black, color.White)
	img := image.NewRGBA(image.Rect(0, 0, 1150, 2050))
	draw.Draw(img, img.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
	draw.Draw(img, text.Bounds().Add(image.Pt(40, 200)), text, image.Point{}, draw.Src)

	if n := len(rowLetters(toGray(img, textDetectSize))); n >= minTextChars {
		t.Fatalf("analysis size already finds %d letters; the test needs text too small for it", n)
	}
	if chars, ok := detectText(img); !ok {
		t.Errorf("detectText() = %d, false; want true", chars)
	}
}

func TestHasTextIgnoresImagesWithoutText(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	grain := image.NewGray(image.Rect(0, 0, 1200, 900)) // a dim, noisy photo
	for i := range grain.Pix {
		grain.Pix[i] = uint8(88 + rng.Intn(81))
	}
	gradient := image.NewGray(image.Rect(0, 0, 1200, 900))
	for y := range 900 {
		for x := range 1200 {
			gradient.Pix[y*1200+x] = uint8((x + y) * 255 / 2100)
		}
	}
	blobs := image.NewGray(image.Rect(0, 0, 1200, 900)) // a few big shapes, like a portrait
	for y := range 900 {
		for x := range 1200 {
			dx, dy := x-600, y-450
			if dx*dx+dy*dy < 300*300 {
				blobs.Pix[y*1200+x] = 200
			} else {
				blobs.Pix[y*1200+x] = 40
			}
		}
	}
	// At full resolution, per-pixel noise passes for small print (one wasted
	// OCR run); camera JPEGs are smoothed, so check it at the analysis size only.
	if n := len(rowLetters(toGray(grain, textDetectSize))); n >= minTextChars {
		t.Errorf("grain: %d letters in rows at the analysis size; want < %d", n, minTextChars)
	}
	tests := map[string]image.Image{
		"plain":    image.NewUniform(color.White),
		"gradient": gradient,
		"shapes":   blobs,
		"tiny":     image.NewGray(image.Rect(0, 0, 10, 10)),
	}
	for name, img := range tests {
		if u, ok := img.(*image.Uniform); ok {
			img = &boundedUniform{u, image.Rect(0, 0, 800, 600)}
		}
		if hasText(img) {
			t.Errorf("%s: hasText = true (%d letters in rows); want false", name, textChars(img))
		}
	}
}

type boundedUniform struct {
	*image.Uniform
	r image.Rectangle
}

func (b *boundedUniform) Bounds() image.Rectangle { return b.r }

func TestToGrayDownscalesYCbCrAndRGBAlike(t *testing.T) {
	rgba := textImage(page, 3, color.Black, color.White).(*image.RGBA)
	ycc := image.NewYCbCr(rgba.Bounds(), image.YCbCrSubsampleRatio444)
	for y := range rgba.Bounds().Dy() {
		for x := range rgba.Bounds().Dx() {
			r, g, b, _ := rgba.At(x, y).RGBA()
			yy, cb, cr := color.RGBToYCbCr(uint8(r>>8), uint8(g>>8), uint8(b>>8))
			ycc.Y[ycc.YOffset(x, y)], ycc.Cb[ycc.COffset(x, y)], ycc.Cr[ycc.COffset(x, y)] = yy, cb, cr
		}
	}

	a, b := toGray(rgba, 500), toGray(ycc, 500)

	if a.w != b.w || a.h != b.h || a.w > 500 || a.w < 400 {
		t.Fatalf("sizes %dx%d and %dx%d; want equal, long side 400-500", a.w, a.h, b.w, b.h)
	}
	for i := range a.pix {
		if d := int(a.pix[i]) - int(b.pix[i]); d < -2 || d > 2 {
			t.Fatalf("pixel %d: %d vs %d", i, a.pix[i], b.pix[i])
		}
	}
}
