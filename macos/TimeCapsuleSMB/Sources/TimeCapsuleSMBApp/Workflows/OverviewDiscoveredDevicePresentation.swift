import Foundation

/// How the overview's Bonjour list shows one discovered device.
struct OverviewDiscoveredDevicePresentation: Equatable {
    let statusText: String
    let isUnsupported: Bool
    /// nil when the row offers no Add button.
    let actionTitle: String?

    init(device: DiscoveredDevice, isSaved: Bool) {
        if device.isUnsupportedModel {
            // An AirPort Express (or another model outside the helper's table)
            // cannot run TimeCapsuleSMB, so it is never offered for adding; a
            // profile saved before this check shows the same status.
            statusText = L10n.string("add_device.state.unsupported")
            isUnsupported = true
            actionTitle = nil
        } else if isSaved {
            statusText = L10n.string("overview.discovery.saved")
            isUnsupported = false
            actionTitle = nil
        } else {
            statusText = L10n.string("overview.discovery.unsaved")
            isUnsupported = false
            actionTitle = L10n.string("overview.discovery.add")
        }
    }
}
