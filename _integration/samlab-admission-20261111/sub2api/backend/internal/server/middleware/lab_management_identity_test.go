package middleware

import "testing"

func TestLabUnmappedAdminManagementBoundary(t *testing.T) {
	for _, path := range []string{"/api/v1/auth/me", "/api/v1/user/totp/setup", "/api/v1/user/password"} {
		if !labManagementIdentityPath(path) {
			t.Fatalf("management identity path denied: %s", path)
		}
	}
	for _, path := range []string{"/api/v1/keys", "/api/v1/usage", "/api/v1/user/profile", "/api/v1/user/totp-bypass"} {
		if labManagementIdentityPath(path) {
			t.Fatalf("business path accepted as management: %s", path)
		}
	}
}
