# US data residency boundary (distractor).
package app.geo.us_boundary

default allow = false

allow {
    input.request.region == "US"
}
