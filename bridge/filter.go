package main

import "strings"

// groupFilter is the allowlist of groups whose messages get stored.
// Entries are group JIDs (…@g.us) or exact group names (case-insensitive).
type groupFilter struct {
	jids  map[string]bool
	names map[string]bool
}

// parseGroupFilter parses a comma-separated list such as "Rowery, 1203…@g.us".
func parseGroupFilter(spec string) groupFilter {
	f := groupFilter{jids: map[string]bool{}, names: map[string]bool{}}
	for _, entry := range strings.Split(spec, ",") {
		entry = strings.TrimSpace(entry)
		switch {
		case entry == "":
		case strings.HasSuffix(entry, "@g.us"):
			f.jids[entry] = true
		default:
			f.names[strings.ToLower(entry)] = true
		}
	}
	return f
}

func (f groupFilter) Allows(jid, name string) bool {
	return f.jids[jid] || (name != "" && f.names[strings.ToLower(name)])
}

func (f groupFilter) Empty() bool {
	return len(f.jids) == 0 && len(f.names) == 0
}
