import SwiftUI

struct OperationTimelineStateIcon: View {
    let state: OperationTimelineItem.State

    var body: some View {
        icon
            .frame(width: 16, height: 16)
            .accessibilityLabel(accessibilityLabel)
    }

    @ViewBuilder
    private var icon: some View {
        switch state {
        case .pending:
            Image(systemName: "circle")
        case .running:
            RotatingTimelineIcon()
        case .succeeded:
            Image(systemName: "checkmark.circle")
        case .warning:
            Image(systemName: "exclamationmark.triangle")
        case .failed:
            Image(systemName: "xmark.octagon")
        }
    }

    private var accessibilityLabel: String {
        switch state {
        case .pending:
            return "Pending"
        case .running:
            return "Running"
        case .succeeded:
            return "Succeeded"
        case .warning:
            return "Warning"
        case .failed:
            return "Failed"
        }
    }
}

private struct RotatingTimelineIcon: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isRotating = false

    var body: some View {
        Image(systemName: "arrow.triangle.2.circlepath")
            .rotationEffect(.degrees(!reduceMotion && isRotating ? 360 : 0))
            .animation(animation, value: isRotating)
            .onAppear {
                guard !reduceMotion else { return }
                isRotating = true
            }
            .onDisappear {
                isRotating = false
            }
    }

    private var animation: Animation? {
        reduceMotion ? nil : .linear(duration: 1).repeatForever(autoreverses: false)
    }
}
