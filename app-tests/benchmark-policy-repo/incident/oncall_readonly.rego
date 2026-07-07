# On-call read-only access (near-miss: on-call but not break-glass).
package app.incident.oncall_readonly

default allow = false

allow {
    input.actor.class == "oncall_responder"
    input.request.mode == "read"
}
