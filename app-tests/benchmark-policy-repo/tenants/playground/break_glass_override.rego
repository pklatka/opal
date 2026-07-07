# Playground tenant override for incident drills (must be excluded from prod selection).
package app.tenants.playground.break_glass_override

default allow = false

allow {
    input.tenant == "playground"
    input.incident.severity == "sev-1"
    input.flags.emergency_override == true
}
