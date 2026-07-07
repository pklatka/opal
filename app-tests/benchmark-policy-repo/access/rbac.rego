# Baseline RBAC for production APIs (benchmark distractor).
package app.access.rbac

default allow = false

allow {
    input.user.role == "admin"
}
