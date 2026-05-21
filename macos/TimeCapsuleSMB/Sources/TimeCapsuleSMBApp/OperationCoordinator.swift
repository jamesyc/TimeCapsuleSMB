import Combine
import Foundation

struct ActiveOperation: Equatable, Identifiable {
    let id: UUID
    let operation: String
    let profileID: DeviceProfile.ID?
    let context: DeviceRuntimeContext?

    init(
        id: UUID = UUID(),
        operation: String,
        profileID: DeviceProfile.ID?,
        context: DeviceRuntimeContext?
    ) {
        self.id = id
        self.operation = operation
        self.profileID = profileID
        self.context = context
    }
}

enum OperationStartResult: Equatable {
    case started(ActiveOperation)
    case rejected(String)

    var operation: ActiveOperation? {
        guard case .started(let operation) = self else {
            return nil
        }
        return operation
    }

    var rejectionMessage: String? {
        guard case .rejected(let message) = self else {
            return nil
        }
        return message
    }
}

@MainActor
final class OperationCoordinator: ObservableObject {
    @Published private(set) var activeOperation: ActiveOperation?
    @Published private(set) var activeDeviceID: DeviceProfile.ID?
    @Published private(set) var rejectedOperationMessage: String?

    let backend: BackendClient

    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
        backend.$isRunning
            .sink { [weak self] isRunning in
                guard !isRunning else { return }
                self?.activeOperation = nil
                self?.activeDeviceID = nil
            }
            .store(in: &cancellables)
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        profile: DeviceProfile?,
        password: String? = nil
    ) -> OperationStartResult {
        run(
            operation: operation,
            params: params,
            context: profile?.runtimeContext,
            activeDeviceID: profile?.id,
            password: password
        )
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        context: DeviceRuntimeContext?,
        activeDeviceID: DeviceProfile.ID?,
        password: String? = nil
    ) -> OperationStartResult {
        guard !backend.isRunning else {
            let message = "Another operation is already running."
            rejectedOperationMessage = message
            return .rejected(message)
        }
        var updatedParams = params
        if let password,
           !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           updatedParams["credentials"] == nil {
            updatedParams["credentials"] = .object(["password": .string(password)])
        }
        let activeOperation = ActiveOperation(
            operation: operation,
            profileID: activeDeviceID,
            context: context
        )
        rejectedOperationMessage = nil
        self.activeOperation = activeOperation
        self.activeDeviceID = activeDeviceID
        backend.run(operation: operation, params: updatedParams, context: context)
        return .started(activeOperation)
    }

    func cancel() {
        backend.cancel()
    }

    func clear() {
        backend.clear()
        rejectedOperationMessage = nil
        activeOperation = nil
        activeDeviceID = nil
    }
}
