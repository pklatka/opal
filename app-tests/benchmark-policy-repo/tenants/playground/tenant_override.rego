# Playground tenant override (excluded from production policy selection).
package app.tenants.playground

default allow = false

allow {
    input.tenant == "playground"
}
