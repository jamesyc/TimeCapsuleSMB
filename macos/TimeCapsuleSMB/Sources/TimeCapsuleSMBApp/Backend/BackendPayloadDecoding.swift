import Foundation

enum BackendContractError: Error, Equatable, LocalizedError {
    case missingPayload(operation: String)
    case payloadDecodeFailed(operation: String, payloadType: String, message: String)

    var errorDescription: String? {
        switch self {
        case .missingPayload(let operation):
            return "\(operation) result did not include a payload."
        case .payloadDecodeFailed(let operation, let payloadType, let message):
            return "\(operation) payload could not be decoded as \(payloadType): \(message)"
        }
    }
}

extension JSONValue {
    func decode<T: Decodable>(_ type: T.Type = T.self) throws -> T {
        let data = try JSONEncoder().encode(self)
        return try JSONDecoder().decode(T.self, from: data)
    }
}

extension BackendEvent {
    func decodePayload<T: Decodable>(_ type: T.Type = T.self) throws -> T {
        guard let payload else {
            throw BackendContractError.missingPayload(operation: operation)
        }
        do {
            return try payload.decode(type)
        } catch let error as DecodingError {
            throw BackendContractError.payloadDecodeFailed(
                operation: operation,
                payloadType: String(describing: type),
                message: error.localizedDescription
            )
        } catch {
            throw BackendContractError.payloadDecodeFailed(
                operation: operation,
                payloadType: String(describing: type),
                message: error.localizedDescription
            )
        }
    }
}
