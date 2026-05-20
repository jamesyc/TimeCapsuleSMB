import Foundation

struct OperationStageState: Equatable {
    let operation: String
    let stage: String
    let risk: String?
    let cancellable: Bool?
    let description: String?

    init?(event: BackendEvent) {
        guard event.type == "stage", let stage = event.stage else {
            return nil
        }
        self.operation = event.operation
        self.stage = stage
        self.risk = event.risk
        self.cancellable = event.cancellable
        self.description = event.description
    }
}

struct BackendErrorViewModel: Equatable {
    let operation: String
    let code: String
    let message: String
    let recovery: BackendRecoveryPayload?

    init(event: BackendEvent) {
        self.operation = event.operation
        self.code = event.code ?? "operation_failed"
        self.message = event.message ?? event.summary
        self.recovery = try? event.recovery?.decode(BackendRecoveryPayload.self)
    }

    init(operation: String, code: String, message: String, recovery: BackendRecoveryPayload? = nil) {
        self.operation = operation
        self.code = code
        self.message = message
        self.recovery = recovery
    }
}

extension BackendEvent {
    var payloadSummaryText: String? {
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
