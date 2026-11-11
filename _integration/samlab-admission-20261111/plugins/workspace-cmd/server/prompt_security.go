package main

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

var protectedSpanPatterns = []*regexp.Regexp{
	regexp.MustCompile("(?s)```.*?```"),
	regexp.MustCompile("`[^`\\r\\n]+`"),
	regexp.MustCompile(`https?://[^\s<>"'，。；、]+`),
	regexp.MustCompile(`(?i)\b[A-Z]:\\(?:[^\s<>:"|?*]+\\)*[^\s<>:"|?*]*`),
	regexp.MustCompile(`/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+`),
	regexp.MustCompile(`--[A-Za-z0-9][A-Za-z0-9_-]*(?:=[^\s，。；、]+)?`),
	regexp.MustCompile(`\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b`),
	regexp.MustCompile(`\b[A-Za-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\b`),
	regexp.MustCompile(`\b[A-Z][A-Z0-9_]{2,}\b`),
	regexp.MustCompile(`"[^"\r\n]+"`),
	regexp.MustCompile(`“[^”\r\n]+”`),
	regexp.MustCompile(`\b\d{4}-\d{2}-\d{2}\b`),
	regexp.MustCompile(`(?i)\bv\d+(?:\.\d+)*\b`),
	regexp.MustCompile(`\b\d+(?:\.\d+)?\b`),
}

var structuralNumberPattern = regexp.MustCompile(`(?m)^[\t ]*(\d+)[.)][\t ]+`)
var numericProtectedSpanPattern = regexp.MustCompile(`^\d+(?:\.\d+)?$`)

func protectedSpans(value string) []string {
	seen := make(map[string]struct{})
	for _, pattern := range protectedSpanPatterns {
		for _, span := range pattern.FindAllString(value, -1) {
			span = strings.TrimSpace(span)
			if span != "" {
				seen[span] = struct{}{}
			}
		}
	}
	spans := make([]string, 0, len(seen))
	for span := range seen {
		spans = append(spans, span)
	}
	sort.Strings(spans)
	return spans
}

func validateProtectedSpans(original, optimized string) []string {
	return validateProtectedSpansWithEvidence(original, optimized, nil)
}

func validateProtectedSpansWithEvidence(original, optimized string, evidence []string) []string {
	required := make(map[string]int)
	for _, span := range protectedSpans(original) {
		required[span]++
	}
	actual := make(map[string]int)
	for _, span := range protectedSpans(optimized) {
		actual[span]++
	}
	failures := make([]string, 0)
	allowedEvidence := make(map[string]int)
	for _, value := range evidence {
		for _, span := range protectedSpans(value) {
			allowedEvidence[span]++
		}
	}
	for span, count := range required {
		if actual[span] < count {
			failures = append(failures, fmt.Sprintf("missing %s (need %d, found %d)", span, count, actual[span]))
		}
	}
	originalStructure := structuralNumberCounts(original)
	optimizedStructure := structuralNumberCounts(optimized)
	for span, count := range actual {
		allowed := required[span] + allowedEvidence[span]
		if numericProtectedSpanPattern.MatchString(span) && optimizedStructure[span] > originalStructure[span] {
			allowed += optimizedStructure[span] - originalStructure[span]
		}
		if count > allowed {
			failures = append(failures, fmt.Sprintf("unexpected %s (allowed %d, found %d)", span, allowed, count))
		}
	}
	sort.Strings(failures)
	return failures
}

func structuralNumberCounts(value string) map[string]int {
	counts := make(map[string]int)
	for _, match := range structuralNumberPattern.FindAllStringSubmatch(value, -1) {
		counts[match[1]]++
	}
	return counts
}
