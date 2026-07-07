package brokeraddr

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
)

const (
	DefaultPort      = 7890
	DefaultTokenFile = "/tmp/wuphf-broker-token"
)

func ResolveBaseURL() string {
	if base := envBaseURL(); base != "" {
		return base
	}
	return fmt.Sprintf("http://127.0.0.1:%d", ResolvePort())
}

func ResolvePort() int {
	for _, key := range []string{"WUPHF_BROKER_PORT", "NEX_BROKER_PORT"} {
		if port := parsePort(os.Getenv(key)); port > 0 {
			return port
		}
	}
	if port := portFromBaseURL(envBaseURL()); port > 0 {
		return port
	}
	return DefaultPort
}

func ResolveTokenFile() string {
	for _, key := range []string{"WUPHF_BROKER_TOKEN_FILE", "NEX_BROKER_TOKEN_FILE"} {
		if path := strings.TrimSpace(os.Getenv(key)); path != "" {
			return path
		}
	}
	port := ResolvePort()
	if port == DefaultPort {
		return DefaultTokenFile
	}
	return fmt.Sprintf("/tmp/wuphf-broker-token-%d", port)
}

func parsePort(raw string) int {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}
	port, err := strconv.Atoi(raw)
	if err != nil || port <= 0 {
		return 0
	}
	return port
}

func envBaseURL() string {
	for _, key := range []string{
		"WUPHF_BROKER_BASE_URL",
		"NEX_BROKER_BASE_URL",
		"WUPHF_TEAM_BROKER_URL",
		"NEX_TEAM_BROKER_URL",
	} {
		if base := strings.TrimSpace(os.Getenv(key)); base != "" {
			return strings.TrimRight(base, "/")
		}
	}
	return ""
}

func portFromBaseURL(base string) int {
	base = strings.TrimSpace(base)
	if base == "" {
		return 0
	}
	parsed, err := url.Parse(base)
	if err != nil || parsed == nil {
		return 0
	}
	return parsePort(parsed.Port())
}
