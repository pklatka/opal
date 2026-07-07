# Shared time helpers (excluded from policy selection tests).
package shared.time_windows

within_business_hours(t) {
    t.hour >= 9
    t.hour < 17
}
