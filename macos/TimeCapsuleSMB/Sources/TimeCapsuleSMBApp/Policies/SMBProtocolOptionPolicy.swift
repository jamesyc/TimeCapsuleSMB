import Foundation

enum SMBProtocolOptionPolicy {
    static func allowsAnyProtocol(requireSMBEncryption: Bool) -> Bool {
        !requireSMBEncryption
    }

    static func allowsRequireSMBEncryption(anyProtocol: Bool, disableSMBEncryption: Bool) -> Bool {
        !anyProtocol && !disableSMBEncryption
    }

    static func allowsDisableSMBEncryption(requireSMBEncryption: Bool) -> Bool {
        !requireSMBEncryption
    }
}
