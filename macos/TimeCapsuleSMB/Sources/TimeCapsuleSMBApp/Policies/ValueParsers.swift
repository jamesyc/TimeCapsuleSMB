import Foundation

enum ValueParsers {
    static func nonNegativeInteger(_ text: String) -> Int? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Int(trimmed), value >= 0 else {
            return nil
        }
        return value
    }
}
