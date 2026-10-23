package main

import "time"

// debouncer runs fn once, delay after the first of a burst of triggers, in its
// own goroutine. Triggers arriving while fn runs schedule one more run.
type debouncer struct {
	pending chan struct{}
}

func newDebouncer(delay time.Duration, fn func()) *debouncer {
	d := &debouncer{pending: make(chan struct{}, 1)}
	go func() {
		for range d.pending {
			time.Sleep(delay)
			select { // absorb triggers that arrived during the delay
			case <-d.pending:
			default:
			}
			fn()
		}
	}()
	return d
}

// Trigger requests a run; it never blocks.
func (d *debouncer) Trigger() {
	select {
	case d.pending <- struct{}{}:
	default:
	}
}
