enum RebootExecutionOptionPolicy {
    static func allowsNoReboot(noWait: Bool) -> Bool {
        !noWait
    }

    static func allowsNoWait(noReboot: Bool) -> Bool {
        !noReboot
    }

    static func normalized(noReboot: Bool, noWait: Bool) -> (noReboot: Bool, noWait: Bool) {
        noReboot ? (true, false) : (false, noWait)
    }
}
