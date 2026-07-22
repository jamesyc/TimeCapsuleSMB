import Foundation

enum SMBProtocolOptionPolicy {
    static func allowsAnyProtocol(requireSMBEncryption: Bool) -> Bool {
        !requireSMBEncryption
    }

    static func allowsRequireSMBEncryption(anyProtocol: Bool) -> Bool {
        !anyProtocol
    }

    static func allowsRequireSMBEncryption(anyProtocol: Bool, forceDisableSMBSigningAndEncryption: Bool) -> Bool {
        !anyProtocol && !forceDisableSMBSigningAndEncryption
    }

    static func allowsForceDisableSMBSigningAndEncryption(requireSMBEncryption: Bool) -> Bool {
        !requireSMBEncryption
    }
}
