package pliqra

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStableNegotiationAndLegacyFallback(t *testing.T) {
	for _, protocol := range []string{"pliqra/1.0", "pliqra/0.1-draft", "pliqra/2.0", "inconsistent"} {
		t.Run(protocol, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Pliqra-Protocol") != "pliqra/1.0" {
					t.Error("stable version not requested")
				}
				document := map[string]any{"protocol": protocol, "discovery": "/protocol/v1/services", "bind": "/protocol/v1/bindings"}
				if protocol == "inconsistent" {
					document["protocol"] = "pliqra/0.1-draft"
					document["supported_protocols"] = []string{"pliqra/1.0"}
				}
				json.NewEncoder(w).Encode(document)
			}))
			defer server.Close()
			client, _ := New(server.URL, "synthetic-key")
			for attempt := 0; attempt < 2; attempt++ {
				_, err := client.Metadata(context.Background())
				rejected := protocol == "pliqra/2.0" || protocol == "inconsistent"
				if (err != nil) != rejected {
					t.Fatalf("wrong protocol decision: %v", err)
				}
			}
		})
	}
}

func TestSSEFramingAndIncompleteTerminal(t *testing.T) {
	for _, ending := range []string{"\n", "\r\n", "\r"} {
		t.Run(fmt.Sprintf("framing-%q", ending), func(t *testing.T) {
			raw := "\ufeff: keepalive" + ending + "data: {\"choices\":" + ending + "data: [{\"delta\":{\"content\":\"hello 🌍\"}}]}" + ending + ending + "data: [DONE]" + ending + ending
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				for i := range []byte(raw) {
					w.Write([]byte(raw)[i : i+1])
					w.(http.Flusher).Flush()
				}
			}))
			defer server.Close()
			client, _ := New(server.URL, "synthetic-key")
			bound := Connection{Client: client, Binding: Binding{Service: "test", Invoke: "/invoke"}}
			items := 0
			err := bound.Stream(context.Background(), nil, nil, func(item map[string]any) error {
				items++
				b, _ := json.Marshal(item)
				if !strings.Contains(string(b), "hello 🌍") {
					t.Fatal(string(b))
				}
				return nil
			})
			if err != nil || items != 1 {
				t.Fatalf("items=%d error=%v", items, err)
			}
		})
	}
	for _, ending := range []string{"", "\n", "\r\n", "\r"} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "data: [DONE]"+ending) }))
		client, _ := New(server.URL, "synthetic-key")
		bound := Connection{Client: client, Binding: Binding{Service: "test", Invoke: "/invoke"}}
		if bound.Stream(context.Background(), nil, nil, func(map[string]any) error { return nil }) == nil {
			t.Errorf("accepted incomplete terminal %q", ending)
		}
		server.Close()
	}
}

func TestProtocolAndFragmentedUnicode(t *testing.T) {
	changed := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer synthetic-key" {
			t.Error("missing authorization")
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/pliqra":
			fmt.Fprint(w, `{"protocol":"pliqra/0.1-draft","discovery":"/protocol/v1/services","bind":"/protocol/v1/bindings"}`)
		case "/protocol/v1/services":
			fmt.Fprint(w, `{"services":[{"name":"synthetic"}]}`)
		case "/protocol/v1/bindings":
			fmt.Fprint(w, `{"id":"binding","service":"synthetic","fingerprint":"fp","expires":9999999999,"invoke":"/protocol/v1/bindings/binding/invoke","extensions":[]}`)
		default:
			if changed {
				w.WriteHeader(409)
				fmt.Fprint(w, `{"detail":"Contract changed"}`)
				return
			}
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			if body["stream"] == true {
				w.Header().Set("Content-Type", "text/event-stream")
				for _, part := range []string{"data: {\"choices\":[{\"delta\":{\"content\":\"world 🌍\"}}]}\n\n", "data: [DONE]\n\n"} {
					fmt.Fprint(w, part)
					w.(http.Flusher).Flush()
				}
			} else {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"Synthetic"}}]}`)
			}
		}
	}))
	defer server.Close()
	client, err := New(server.URL, "synthetic-key")
	if err != nil {
		t.Fatal(err)
	}
	services, err := client.Services(context.Background())
	if err != nil || len(services) != 1 {
		t.Fatal(services, err)
	}
	bound, err := client.Connect(context.Background(), "synthetic", ConnectOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = bound.Complete(context.Background(), []map[string]any{{"role": "user", "content": "Synthetic"}}, nil); err != nil {
		t.Fatal(err)
	}
	text := ""
	err = bound.Stream(context.Background(), nil, nil, func(item map[string]any) error { raw, _ := json.Marshal(item); text += string(raw); return nil })
	if err != nil || !strings.Contains(text, "world 🌍") {
		t.Fatal(text, err)
	}
	changed = true
	_, err = bound.Complete(context.Background(), nil, nil)
	typed, ok := err.(*Error)
	if !ok || !typed.ContractChanged() {
		t.Fatal(err)
	}
	if _, err = client.Request(context.Background(), "//attacker.test/", nil); err == nil {
		t.Fatal("accepted non-local path")
	}
}

func TestTruncatedStreamAndRedirectAreRejected(t *testing.T) {
	targetRequests := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { targetRequests++ }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirect" {
			http.Redirect(w, r, target.URL, 302)
			return
		}
		fmt.Fprint(w, "data: {\"choices\":[]}\n\n")
	}))
	defer server.Close()
	client, _ := New(server.URL, "synthetic-key")
	if _, err := client.Request(context.Background(), "/redirect", nil); err == nil {
		t.Fatal("redirect was followed")
	}
	if targetRequests != 0 {
		t.Fatal("redirect received application credentials")
	}
	bound := Connection{Client: client, Binding: Binding{Service: "synthetic", Invoke: "/invoke"}}
	if err := bound.Stream(context.Background(), nil, nil, func(map[string]any) error { return nil }); err == nil {
		t.Fatal("truncated stream was accepted")
	}
}
