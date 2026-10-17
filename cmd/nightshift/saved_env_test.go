package main

import (
	"strings"
	"testing"
)

func TestWithSavedEnvAddsMissingSavedVariablesOnly(t *testing.T) {
	got := withSavedEnv([]string{"PATH=inherited", "NIGHTSHIFT_TEST_ONLY=1"})
	values := map[string]string{}
	for _, kv := range got {
		k, v, _ := strings.Cut(kv, "=")
		values[strings.ToUpper(k)] = v
	}
	if values["PATH"] != "inherited" {
		t.Fatalf("an inherited variable must not be replaced: PATH=%q", values["PATH"])
	}
	temp, ok := values["TEMP"]
	if !ok {
		t.Fatal("TEMP, saved in the user environment, was not added")
	}
	if strings.Contains(temp, "%") {
		t.Fatalf("TEMP was not expanded: %q", temp)
	}
}
