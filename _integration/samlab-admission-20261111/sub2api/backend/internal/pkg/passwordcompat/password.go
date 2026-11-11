// Package passwordcompat preserves bcrypt accounts and supports long passwords without truncation.
package passwordcompat

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"strings"

	"golang.org/x/crypto/argon2"
	"golang.org/x/crypto/bcrypt"
)

func Hash(password string) (string, error) {
	if len(password) <= 72 {
		hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
		return string(hash), err
	}
	if len(password) > 512 {
		return "", errors.New("password exceeds supported length")
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	key := argon2.IDKey([]byte(password), salt, 3, 32*1024, 1, 32)
	return "$argon2id$v=19$m=32768,t=3,p=1$" + base64.RawStdEncoding.EncodeToString(salt) + "$" + base64.RawStdEncoding.EncodeToString(key), nil
}

func Matches(password, hash string) bool {
	if !strings.HasPrefix(hash, "$argon2id$") {
		if len(password) > 72 {
			return false
		}
		return bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) == nil
	}
	parts := strings.Split(hash, "$")
	if len(parts) != 6 || parts[2] != "v=19" || parts[3] != "m=32768,t=3,p=1" || len(password) > 512 {
		return false
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil || len(salt) != 16 {
		return false
	}
	key, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil || len(key) != 32 {
		return false
	}
	got := argon2.IDKey([]byte(password), salt, 3, 32*1024, 1, 32)
	return subtle.ConstantTimeCompare(key, got) == 1
}
