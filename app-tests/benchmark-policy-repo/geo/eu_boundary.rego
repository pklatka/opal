# EU data residency boundary (benchmark: production bundle distractor).
package app.geo.eu_boundary

default allow = false

allow {
    input.request.region == "EU"
}
