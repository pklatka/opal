# PII read-only enforcement (distractor).
package app.compliance.pii_readonly

default allow = false

allow {
    input.request.sensitivity == "pii"
    input.request.mode == "read"
}
