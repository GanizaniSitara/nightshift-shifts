// Package nightshiftassets embeds the crew manifests, briefs and personas so
// nightshift.exe runs standalone; files beside the executable override them.
package nightshiftassets

import "embed"

//go:embed crews/*.yaml briefs/*.md personas/*.yaml briefs/legacy/*.md personas/legacy/*.yaml
var Files embed.FS
