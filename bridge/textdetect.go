package main

import (
	"image"
	"sort"
)

// Fast check whether a photo contains text, so that OCR (about a second per
// image, more for big photos) runs only on images likely to have some. It
// looks for rows of letters: blobs that stand out from their surroundings
// (dark on light or light on dark), have the size and shape of a character,
// and sit next to blobs of similar height on the same line. Faces, landscapes
// and textures give few such rows. It errs on the side of "has text": a false
// positive costs one OCR run, whose low-confidence output is then dropped.

const (
	textDetectSize = 1000 // long side the image is scaled to before analysis
	minTextChars   = 10   // letters in rows needed to call an image "has text"
	minRowChars    = 5    // shortest row of letters that counts
	contrastDelta  = 18   // how much darker/lighter than its neighbourhood a letter pixel is

	// Small print (a phone screenshot of a long text, a dense notice) is too
	// small to see at the analysis size, so images without text there are
	// checked again at up to this size. Photos have more letter-like detail at
	// that size too, so it takes more letters to count. Tuned on real class
	// group photos (October 2026): no text missed, 2 in 112 extra OCR runs.
	fineDetectSize = 2400
	minFineChars   = 60
)

// grayImage is an 8-bit luminance raster.
type grayImage struct {
	w, h int
	pix  []uint8
}

// detectText reports whether img probably contains text worth OCR-ing, and
// the number of letters in rows it found. It looks at the analysis size, at
// 40% of it (big letters, e.g. a poster title or a table header) and, when
// neither finds text, at fine size for small print.
func detectText(img image.Image) (chars int, ok bool) {
	g := toGray(img, textDetectSize)
	n := len(rowLetters(g))
	if n >= minTextChars {
		return n, true
	}
	n = max(n, len(rowLetters(toGray(img, textDetectSize*2/5))))
	if n >= minTextChars {
		return n, true
	}
	if fine := toGray(img, fineDetectSize); fine.w > g.w {
		if f := len(rowLetters(fine)); f >= minFineChars {
			return f, true
		}
	}
	return n, false
}

// hasText reports whether img probably contains text worth OCR-ing.
func hasText(img image.Image) bool {
	_, ok := detectText(img)
	return ok
}

// textChars is the number of letters in rows detectText found in img.
func textChars(img image.Image) int {
	n, _ := detectText(img)
	return n
}

// toGray converts img to grayscale, box-averaged down so the long side is at most maxSide.
func toGray(img image.Image, maxSide int) grayImage {
	b := img.Bounds()
	factor := max((max(b.Dx(), b.Dy())+maxSide-1)/maxSide, 1)
	g := grayImage{w: b.Dx() / factor, h: b.Dy() / factor}
	g.pix = make([]uint8, g.w*g.h)
	luma := func(x, y int) uint32 {
		if ycc, ok := img.(*image.YCbCr); ok { // what image/jpeg decodes to
			return uint32(ycc.Y[ycc.YOffset(x, y)])
		}
		r, gr, bl, _ := img.At(x, y).RGBA()
		return (299*r + 587*gr + 114*bl) / 1000 >> 8
	}
	for y := 0; y < g.h; y++ {
		for x := 0; x < g.w; x++ {
			var sum uint32
			for dy := 0; dy < factor; dy++ {
				for dx := 0; dx < factor; dx++ {
					sum += luma(b.Min.X+x*factor+dx, b.Min.Y+y*factor+dy)
				}
			}
			g.pix[y*g.w+x] = uint8(sum / uint32(factor*factor))
		}
	}
	return g
}

// rowLetters returns the blobs of g that look like letters in rows of text.
func rowLetters(g grayImage) []blob {
	if g.w < 32 || g.h < 32 {
		return nil
	}
	dark, light := contrastMasks(g, 15)
	return append(letterRows(components(dark, g.w, g.h), g.h), letterRows(components(light, g.w, g.h), g.h)...)
}

// contrastMasks marks pixels clearly darker (dark) or lighter (light) than the
// mean of the (2r+1)x(2r+1) window around them.
func contrastMasks(g grayImage, r int) (dark, light []bool) {
	// Integral image: sum[y][x] = sum of pix above and left of (x, y).
	w1 := g.w + 1
	sum := make([]int64, w1*(g.h+1))
	for y := 0; y < g.h; y++ {
		var row int64
		for x := 0; x < g.w; x++ {
			row += int64(g.pix[y*g.w+x])
			sum[(y+1)*w1+x+1] = sum[y*w1+x+1] + row
		}
	}
	dark, light = make([]bool, len(g.pix)), make([]bool, len(g.pix))
	for y := 0; y < g.h; y++ {
		y0, y1 := max(y-r, 0), min(y+r+1, g.h)
		for x := 0; x < g.w; x++ {
			x0, x1 := max(x-r, 0), min(x+r+1, g.w)
			s := sum[y1*w1+x1] - sum[y0*w1+x1] - sum[y1*w1+x0] + sum[y0*w1+x0]
			mean := int(s / int64((y1-y0)*(x1-x0)))
			v := int(g.pix[y*g.w+x])
			dark[y*g.w+x] = v < mean-contrastDelta
			light[y*g.w+x] = v > mean+contrastDelta
		}
	}
	return dark, light
}

// letterRows returns the character-like blobs that are in a straight row of at
// least minRowChars blobs of similar height, each next to the previous one.
func letterRows(blobs []blob, imgH int) []blob {
	var chars []blob
	for _, b := range blobs {
		w, h := b.x1-b.x0+1, b.y1-b.y0+1
		fill := float64(b.pixels) / float64(w*h)
		if h >= 7 && h <= imgH/3 && w <= 2*h && h <= 6*w && b.pixels >= 12 && fill >= 0.12 && fill <= 0.85 {
			chars = append(chars, b)
		}
	}
	sort.Slice(chars, func(i, j int) bool { return chars[i].x0 < chars[j].x0 })

	parent := make([]int, len(chars))
	for i := range parent {
		parent[i] = i
	}
	var find func(int) int
	find = func(i int) int {
		if parent[i] != i {
			parent[i] = find(parent[i])
		}
		return parent[i]
	}
	for i, a := range chars {
		ah := a.y1 - a.y0 + 1
		for j := i + 1; j < len(chars) && chars[j].x0 <= a.x1+2*ah; j++ {
			if neighbours(a, chars[j]) {
				parent[find(j)] = find(i)
			}
		}
	}
	type row struct {
		n, y0, y1, heights int
		members            []blob
	}
	rows := map[int]*row{}
	for i, c := range chars {
		r := rows[find(i)]
		if r == nil {
			r = &row{y0: c.y0, y1: c.y1}
			rows[find(i)] = r
		}
		r.n++
		r.y0, r.y1 = min(r.y0, c.y0), max(r.y1, c.y1)
		r.heights += c.y1 - c.y0 + 1
		r.members = append(r.members, c)
	}
	var letters []blob
	for _, r := range rows {
		// A row of letters is straight: not much taller than its letters. Textures
		// (leaves, fur, gravel) chain up in all directions into tall meshes instead.
		if r.n >= minRowChars && (r.y1-r.y0+1)*r.n*2 <= r.heights*3 {
			letters = append(letters, r.members...)
		}
	}
	return letters
}

// neighbours reports whether b (starting right of or at a's left edge) looks
// like the next letter after a on the same line.
func neighbours(a, b blob) bool {
	ah, bh := a.y1-a.y0+1, b.y1-b.y0+1
	hi, lo := max(ah, bh), min(ah, bh)
	if lo*3 < hi*2 { // heights differ by more than a third
		return false
	}
	gap := b.x0 - a.x1 - 1
	if gap > hi || gap < -hi/3 { // too far apart, or overlapping (not side by side)
		return false
	}
	// Same line: vertical centres close, or bottoms aligned (letters with ascenders).
	ca, cb := a.y0+a.y1, b.y0+b.y1 // doubled centres
	return abs(ca-cb) <= hi*2/3 || abs(a.y1-b.y1) <= hi/4
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

type blob struct {
	x0, y0, x1, y1 int
	pixels         int
}

// components returns the 8-connected blobs of set pixels (8, so that thin
// diagonal strokes of small letters stay in one piece).
func components(m []bool, w, h int) []blob {
	seen := make([]bool, len(m))
	var blobs []blob
	var stack []int
	for start := range m {
		if !m[start] || seen[start] {
			continue
		}
		b := blob{x0: w, y0: h, x1: -1, y1: -1}
		seen[start] = true
		stack = append(stack[:0], start)
		for len(stack) > 0 {
			i := stack[len(stack)-1]
			stack = stack[:len(stack)-1]
			x, y := i%w, i/w
			b.pixels++
			b.x0, b.x1 = min(b.x0, x), max(b.x1, x)
			b.y0, b.y1 = min(b.y0, y), max(b.y1, y)
			for dy := -1; dy <= 1; dy++ {
				for dx := -1; dx <= 1; dx++ {
					nx, ny := x+dx, y+dy
					if nx < 0 || nx >= w || ny < 0 || ny >= h {
						continue
					}
					if n := ny*w + nx; m[n] && !seen[n] {
						seen[n] = true
						stack = append(stack, n)
					}
				}
			}
		}
		blobs = append(blobs, b)
	}
	return blobs
}
