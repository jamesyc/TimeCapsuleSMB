import Foundation

enum ValueParsers {
    static func discoveryTimeout(_ text: String) -> Double? {
        guard let value = nonNegativeDouble(text), value >= 5 else { return nil }
        return value
    }

    static func nonNegativeInteger(_ text: String) -> Int? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Int(trimmed), value >= 0 else {
            return nil
        }
        return value
    }

    /// The device's longest idle time: samba/config.c runs a larger one as
    /// 300 seconds, so the app never saves or sends one.
    static let maxAtaIdleSeconds = 86_400

    static func ataIdleSeconds(_ text: String) -> Int? {
        guard let value = nonNegativeInteger(text), value <= maxAtaIdleSeconds else {
            return nil
        }
        return value
    }

    static func nonNegativeDouble(_ text: String) -> Double? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Double(trimmed), value.isFinite, value >= 0 else {
            return nil
        }
        return value
    }
}
