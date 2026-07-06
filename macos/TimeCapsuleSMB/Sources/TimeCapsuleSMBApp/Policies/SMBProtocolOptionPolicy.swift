import Foundation

enum SMBProtocolOptionPolicy {
    static func allowsAnyProtocol(requireSMBEncryption: Bool) -> Bool {
        !requireSMBEncryption
    }

    static func allowsRequireSMBEncryption(anyProtocol: Bool) -> Bool {
        !anyProtocol
    }
}
