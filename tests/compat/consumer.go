package main

import (
	pliqra "compatclient"
	"context"
	"encoding/json"
	"os"
)

func main() {
	var config struct {
		URL      string `json:"url"`
		Token    string `json:"token"`
		Service  string `json:"service"`
		Protocol string `json:"protocol"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&config); err != nil {
		panic(err)
	}
	client, err := pliqra.New(config.URL, config.Token)
	if err != nil {
		panic(err)
	}
	ctx := context.Background()
	metadata, err := client.Metadata(ctx)
	if err != nil {
		panic(err)
	}
	if metadata.Protocol != config.Protocol {
		panic("Unexpected negotiated protocol")
	}
	binding, err := client.Connect(ctx, config.Service, pliqra.ConnectOptions{Requirements: []string{"chat", "streaming"}})
	if err != nil {
		panic(err)
	}
	messages := []map[string]any{{"role": "user", "content": "Synthetic compatibility check"}}
	if _, err = binding.Complete(ctx, messages, nil); err != nil {
		panic(err)
	}
	chunks := 0
	err = binding.Stream(ctx, messages, nil, func(chunk map[string]any) error { chunks++; return nil })
	if err != nil {
		panic(err)
	}
	if chunks == 0 {
		panic("Empty stream")
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"passed": true, "protocol": metadata.Protocol, "completion": true, "stream": true})
}
