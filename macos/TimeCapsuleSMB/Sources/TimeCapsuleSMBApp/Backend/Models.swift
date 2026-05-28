import Foundation

public enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
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

    public func encode(to encoder: Encoder) throws {
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

    public var displayText: String {
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

    public func stringValue(for key: String) -> String? {
        guard case .object(let values) = self, case .string(let value)? = values[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }
}

public struct BackendEvent: Decodable, Identifiable, Sendable {
    public let id = UUID()
    public let schemaVersion: Int?
    public let requestId: String?
    public let type: String
    public let operation: String
    public let code: String?
    public let stage: String?
    public let level: String?
    public let message: String?
    public let status: String?
    public let ok: Bool?
    public let payload: JSONValue?
    public let details: JSONValue?
    public let debug: JSONValue?
    public let recovery: JSONValue?
    public let risk: String?
    public let cancellable: Bool?
    public let description: String?

    public init(
        schemaVersion: Int? = 1,
        requestId: String? = UUID().uuidString,
        type: String,
        operation: String,
        code: String? = nil,
        stage: String? = nil,
        level: String? = nil,
        message: String? = nil,
        status: String? = nil,
        ok: Bool? = nil,
        payload: JSONValue? = nil,
        details: JSONValue? = nil,
        debug: JSONValue? = nil,
        recovery: JSONValue? = nil,
        risk: String? = nil,
        cancellable: Bool? = nil,
        description: String? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.requestId = requestId
        self.type = type
        self.operation = operation
        self.code = code
        self.stage = stage
        self.level = level
        self.message = message
        self.status = status
        self.ok = ok
        self.payload = payload
        self.details = details
        self.debug = debug
        self.recovery = recovery
        self.risk = risk
        self.cancellable = cancellable
        self.description = description
    }

    public static func error(
        operation: String,
        code: String,
        message: String,
        requestId: String? = UUID().uuidString,
        debug: JSONValue? = nil
    ) -> BackendEvent {
        BackendEvent(
            requestId: requestId,
            type: "error",
            operation: operation,
            code: code,
            message: message,
            debug: debug
        )
    }

    public func withRequestId(_ requestId: String) -> BackendEvent {
        BackendEvent(
            schemaVersion: schemaVersion,
            requestId: requestId,
            type: type,
            operation: operation,
            code: code,
            stage: stage,
            level: level,
            message: message,
            status: status,
            ok: ok,
            payload: payload,
            details: details,
            debug: debug,
            recovery: recovery,
            risk: risk,
            cancellable: cancellable,
            description: description
        )
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case requestId = "request_id"
        case type
        case operation
        case code
        case stage
        case level
        case message
        case status
        case ok
        case payload
        case details
        case debug
        case recovery
        case risk
        case cancellable
        case description
    }

    public var summary: String {
        switch type {
        case "stage":
            return stage.map { L10n.format("event.summary.stage", operation, $0) } ?? operation
        case "check":
            return L10n.format(
                "event.summary.check",
                status ?? L10n.string("event.summary.check.default_status"),
                message ?? ""
            )
        case "result":
            if let payloadSummary = payloadSummary {
                return payloadSummary
            }
            let result = ok == true
                ? L10n.string("event.summary.result.finished")
                : L10n.string("event.summary.result.failed")
            return L10n.format("event.summary.result", operation, result)
        case "error":
            return L10n.format(
                "event.summary.error",
                operation,
                message ?? L10n.string("event.summary.error.default_message")
            )
        default:
            return message ?? stage ?? operation
        }
    }

    private var payloadSummary: String? {
        guard let payload else {
            return nil
        }
        for key in ["summary", "message", "summary_text"] {
            if let value = payload.stringValue(for: key) {
                return value
            }
        }
        return nil
    }
}
