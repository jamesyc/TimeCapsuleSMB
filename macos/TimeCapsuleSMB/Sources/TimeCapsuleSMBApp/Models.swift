import Foundation

enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            self = .array(try container.decode([JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var displayText: String {
        switch self {
        case .string(let value):
            return value
        case .number(let value):
            return String(value)
        case .bool(let value):
            return value ? "true" : "false"
        case .object, .array:
            guard
                let data = try? JSONEncoder().encode(self),
                let text = String(data: data, encoding: .utf8)
            else {
                return ""
            }
            return text
        case .null:
            return "null"
        }
    }
}

struct BackendEvent: Decodable, Identifiable {
    let id = UUID()
    let type: String
    let operation: String
    let stage: String?
    let level: String?
    let message: String?
    let status: String?
    let ok: Bool?
    let payload: JSONValue?
    let details: JSONValue?
    let debug: JSONValue?

    enum CodingKeys: String, CodingKey {
        case type
        case operation
        case stage
        case level
        case message
        case status
        case ok
        case payload
        case details
        case debug
    }

    var summary: String {
        switch type {
        case "stage":
            return stage.map { "\(operation): \($0)" } ?? operation
        case "check":
            return "\(status ?? "INFO") \(message ?? "")"
        case "result":
            return "\(operation): \(ok == true ? "finished" : "failed")"
        case "error":
            return "\(operation): \(message ?? "error")"
        default:
            return message ?? stage ?? operation
        }
    }
}

