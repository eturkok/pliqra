// Package pliqra implements the Pliqra protocol without gateway dependencies.
package pliqra

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Error struct {
	Status int
	Detail any
}

func (e *Error) Error() string         { return fmt.Sprintf("Pliqra HTTP %d: %v", e.Status, e.Detail) }
func (e *Error) ContractChanged() bool { return e.Status == http.StatusConflict }

type Client struct {
	BaseURL string
	APIKey  string
	HTTP    *http.Client
}
type Metadata struct {
	Protocol  string `json:"protocol"`
	Discovery string `json:"discovery"`
	Bind      string `json:"bind"`
}
type Binding struct {
	Placement   map[string]any `json:"placement,omitempty"`
	ID          string         `json:"id"`
	Service     string         `json:"service"`
	Fingerprint string         `json:"fingerprint"`
	Expires     float64        `json:"expires"`
	Invoke      string         `json:"invoke"`
	Extensions  []string       `json:"extensions"`
}
type Connection struct {
	Client  *Client
	Binding Binding
}
type ConnectOptions struct {
	Requirements       []string
	TTLSeconds         int
	RequiredExtensions []string
	OptionalExtensions []string
}

func New(baseURL, apiKey string) (*Client, error) {
	parsed, err := url.Parse(baseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("use an HTTP(S) base URL without credentials, query or fragment")
	}
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), APIKey: apiKey, HTTP: &http.Client{Timeout: 90 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func localPath(path string) bool {
	return strings.HasPrefix(path, "/") && !strings.HasPrefix(path, "//") && !strings.Contains(path, "://") && !strings.Contains(path, "\\")
}
func (c *Client) Request(ctx context.Context, path string, body any) (*http.Response, error) {
	if !localPath(path) {
		return nil, fmt.Errorf("server returned a non-local protocol path")
	}
	method := http.MethodGet
	var input io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		input = bytes.NewReader(raw)
		method = http.MethodPost
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, input)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.APIKey)
	req.Header.Set("Content-Type", "application/json")
	// Clone the transport options so injected clients cannot accidentally follow a
	// discovery redirect carrying the application key.
	transport := *c.HTTP
	transport.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	response, err := transport.Do(req)
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		defer response.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 200001))
		var document map[string]any
		detail := any("Protocol request failed")
		if len(raw) <= 200000 && json.Unmarshal(raw, &document) == nil && document["detail"] != nil {
			detail = document["detail"]
		}
		return nil, &Error{Status: response.StatusCode, Detail: detail}
	}
	return response, nil
}
func (c *Client) JSON(ctx context.Context, path string, body any, result any) error {
	response, err := c.Request(ctx, path, body)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 16000001))
	if err != nil {
		return err
	}
	if len(raw) > 16000000 {
		return fmt.Errorf("response exceeded 16 MB")
	}
	return json.Unmarshal(raw, result)
}
func (c *Client) Metadata(ctx context.Context) (Metadata, error) {
	var metadata Metadata
	err := c.JSON(ctx, "/.well-known/pliqra", nil, &metadata)
	if err == nil && metadata.Protocol != "pliqra/0.1-draft" {
		err = fmt.Errorf("unsupported Pliqra protocol version")
	}
	return metadata, err
}
func (c *Client) Services(ctx context.Context) ([]map[string]any, error) {
	metadata, err := c.Metadata(ctx)
	if err != nil {
		return nil, err
	}
	var result struct {
		Services []map[string]any `json:"services"`
	}
	err = c.JSON(ctx, metadata.Discovery, nil, &result)
	return result.Services, err
}
func (c *Client) Connect(ctx context.Context, service string, options ConnectOptions) (*Connection, error) {
	metadata, err := c.Metadata(ctx)
	if err != nil {
		return nil, err
	}
	if options.Requirements == nil {
		options.Requirements = []string{"chat"}
	}
	if options.TTLSeconds == 0 {
		options.TTLSeconds = 3600
	}
	var binding Binding
	err = c.JSON(ctx, metadata.Bind, map[string]any{"service": service, "requirements": options.Requirements, "ttl_seconds": options.TTLSeconds, "required_extensions": nonNil(options.RequiredExtensions), "optional_extensions": nonNil(options.OptionalExtensions)}, &binding)
	if err != nil {
		return nil, err
	}
	for _, extension := range options.RequiredExtensions {
		if !contains(binding.Extensions, extension) {
			return nil, fmt.Errorf("server did not negotiate required extension %s", extension)
		}
	}
	return &Connection{Client: c, Binding: binding}, nil
}
func nonNil(value []string) []string {
	if value == nil {
		return []string{}
	}
	return value
}
func contains(items []string, value string) bool {
	for _, item := range items {
		if item == value {
			return true
		}
	}
	return false
}
func (b *Connection) Complete(ctx context.Context, messages []map[string]any, parameters map[string]any) (map[string]any, error) {
	body := map[string]any{}
	for key, value := range parameters {
		body[key] = value
	}
	body["model"] = b.Binding.Service
	body["messages"] = messages
	body["stream"] = false
	var result map[string]any
	err := b.Client.JSON(ctx, b.Binding.Invoke, body, &result)
	return result, err
}
func (b *Connection) Stream(ctx context.Context, messages []map[string]any, parameters map[string]any, consume func(map[string]any) error) error {
	body := map[string]any{}
	for key, value := range parameters {
		body[key] = value
	}
	body["model"] = b.Binding.Service
	body["messages"] = messages
	body["stream"] = true
	response, err := b.Client.Request(ctx, b.Binding.Invoke, body)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	scanner := bufio.NewScanner(io.LimitReader(response.Body, 16000001))
	scanner.Buffer(make([]byte, 65536), 1000000)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		value := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if value == "[DONE]" {
			return nil
		}
		if value == "" {
			continue
		}
		var item map[string]any
		if err = json.Unmarshal([]byte(value), &item); err != nil {
			return err
		}
		if item["error"] != nil {
			return &Error{Status: 502, Detail: "Upstream stream failed"}
		}
		if err = consume(item); err != nil {
			return err
		}
	}
	if err = scanner.Err(); err != nil {
		return err
	}
	return fmt.Errorf("stream ended without a completion marker")
}
func (b *Connection) Operation(ctx context.Context, operation string, parameters map[string]any) ([]byte, string, error) {
	if !contains(b.Binding.Extensions, "pliqra.operations/1") {
		return nil, "", fmt.Errorf("negotiate pliqra.operations/1 when connecting")
	}
	if operation != "embeddings" && operation != "speech" && operation != "transcription" {
		return nil, "", fmt.Errorf("unsupported operation")
	}
	body := map[string]any{}
	for key, value := range parameters {
		body[key] = value
	}
	body["model"] = b.Binding.Service
	response, err := b.Client.Request(ctx, "/protocol/v1/bindings/"+url.PathEscape(b.Binding.ID)+"/operations/"+operation, body)
	if err != nil {
		return nil, "", err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 32000001))
	if err == nil && len(raw) > 32000000 {
		err = fmt.Errorf("response exceeded 32 MB")
	}
	return raw, response.Header.Get("Content-Type"), err
}
