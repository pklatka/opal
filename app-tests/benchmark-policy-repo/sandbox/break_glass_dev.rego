# Sandbox-only break-glass shortcut for ephemeral dev clusters (must be excluded).
package app.sandbox.break_glass_dev

default allow = false

allow {
    input.cluster.environment == "dev"
    input.incident.active == true
    input.flags.emergency_override == true
}
