import Foundation

struct DeviceProfileTraits: Equatable {
    let isNetBSD4: Bool
    let isNetBSD6: Bool
    let isSupported: Bool
    let supportsFlashBootHook: Bool
    let needsActivationAfterReboot: Bool
}

extension DeviceProfile {
    var traits: DeviceProfileTraits {
        let isNetBSD4 = payloadFamily?.localizedCaseInsensitiveContains("netbsd4") == true
            || osRelease?.hasPrefix("4.") == true
        let isNetBSD6 = payloadFamily?.localizedCaseInsensitiveContains("netbsd6") == true
            || osRelease?.hasPrefix("6.") == true
        let unsupportedValues = [
            payloadFamily,
            deviceGeneration
        ]
        let isSupported = !unsupportedValues.contains { value in
            value?.localizedCaseInsensitiveContains("unsupported") == true
        }
        return DeviceProfileTraits(
            isNetBSD4: isNetBSD4,
            isNetBSD6: isNetBSD6,
            isSupported: isSupported,
            supportsFlashBootHook: isNetBSD4,
            needsActivationAfterReboot: isNetBSD4
        )
    }
}
