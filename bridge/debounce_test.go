package main

import (
	"sync/atomic"
	"testing"
	"time"
)

func TestDebouncerCoalescesBurstsIntoOneCall(t *testing.T) {
	var calls atomic.Int32
	d := newDebouncer(30*time.Millisecond, func() { calls.Add(1) })

	for i := 0; i < 20; i++ { // e.g. a full app-state sync emitting many contact events
		d.Trigger()
	}
	time.Sleep(150 * time.Millisecond)
	if got := calls.Load(); got != 1 {
		t.Fatalf("calls after a burst = %d, want 1", got)
	}

	d.Trigger() // a later change runs again
	time.Sleep(150 * time.Millisecond)
	if got := calls.Load(); got != 2 {
		t.Errorf("calls after a second trigger = %d, want 2", got)
	}
}
