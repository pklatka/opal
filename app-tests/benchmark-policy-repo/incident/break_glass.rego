# Sev-1 break-glass: on-call responders, outside business hours, emergency_override required.
package app.incident.break_glass

default allow = false

# Benchmark intent: this module matches the NL description in opal/test1.
allow {
    input.incident.severity == "sev-1"
    input.actor.class == "oncall_responder"
    input.flags.emergency_override == true
    not input.time.within_business_hours
}
