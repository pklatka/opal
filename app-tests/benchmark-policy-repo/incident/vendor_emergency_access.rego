# Vendor emergency access during incidents (near-miss: emergency wording).
package app.incident.vendor_emergency_access

default allow = false

allow {
    input.actor.class == "vendor"
    input.incident.active == true
}
