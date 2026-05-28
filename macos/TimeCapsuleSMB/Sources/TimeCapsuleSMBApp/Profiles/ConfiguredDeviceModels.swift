import Foundation

struct ConfiguredDeviceState: Equatable {
    let host: String
    let configPath: String
    let configureId: String
    let sshAuthenticated: Bool
    let syap: String?
    let model: String?
    let compatibility: DeviceCompatibilityPayload?

    init(payload: ConfigurePayload) {
        self.host = payload.host
        self.configPath = payload.configPath
        self.configureId = payload.configureId
        self.sshAuthenticated = payload.sshAuthenticated
        self.syap = payload.deviceSyap ?? payload.device?.syap
        self.model = payload.deviceModel ?? payload.device?.model
        self.compatibility = payload.compatibility
    }
}
