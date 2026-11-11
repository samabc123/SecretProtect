package passwordcompat

import (
	"strings"
	"testing"
)

func TestLegacyAndLongPasswords(t *testing.T) {
	for _, value := range []string{"123456", strings.Repeat("a", 72), strings.Repeat("b", 128), strings.Repeat("密", 128)} {
		hash, err := Hash(value)
		if err != nil || !Matches(value, hash) || Matches(value+"x", hash) {
			t.Fatalf("password compatibility failed at length %d", len(value))
		}
		if len(hash) > 255 || strings.Contains(hash, value) {
			t.Fatal("invalid stored password hash")
		}
	}
	if Matches("password", "$argon2id$v=19$m=99999999,t=3,p=1$bad$bad") {
		t.Fatal("unbounded hash parameters must be rejected")
	}
}
