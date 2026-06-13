import Foundation

struct AddDeviceProgressPresentation: Equatable, BlockingProgressPresenting {
    let title: String
    let message: String
    let detail: String?

    init?(state: AddDeviceFlowState, currentStage: OperationStageState?) {
        switch state {
        case .discovering:
            self.title = L10n.string("add_device.progress.discovering.title")
            self.message = L10n.string("add_device.progress.discovering.message")
            self.detail = nil
        case .checkingLocalNetwork:
            self.title = L10n.string("add_device.progress.local_network.title")
            self.message = L10n.string("add_device.progress.local_network.message")
            self.detail = nil
        case .configuring:
            self.title = L10n.string("add_device.progress.configuring.title")
            self.message = L10n.string("add_device.progress.configuring.message")
            self.detail = currentStage.map {
                OperationTimelineBuilder.stageDetail(for: $0.operation, stage: $0.stage, fallback: nil)
                    ?? OperationTimelineBuilder.stageTitle(for: $0.operation, stage: $0.stage)
            }
        case .savingProfile:
            self.title = L10n.string("add_device.progress.saving.title")
            self.message = L10n.string("add_device.progress.saving.message")
            self.detail = nil
        case .idle,
             .discoveryEmpty,
             .discoveryReady,
             .manualEntry,
             .passwordEntry,
             .awaitingConfirmation,
             .saved,
             .authFailed,
             .unsupported,
             .failed:
            return nil
        }
    }
}
