# Shared utilities (excluded from policy selection tests).
package shared.utils

has_topic(topics, name) {
    topics[_] == name
}
