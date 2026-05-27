import SwiftUI

enum OperationTimelineVisualStyle {
    static func symbol(for state: OperationTimelineItem.State) -> String {
        switch state {
        case .pending:
            return "circle"
        case .running:
            return "arrow.triangle.2.circlepath"
        case .succeeded:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.triangle"
        case .failed:
            return "xmark.octagon"
        }
    }

    static func color(for state: OperationTimelineItem.State) -> Color {
        switch state {
        case .pending:
            return .secondary
        case .running:
            return .accentColor
        case .succeeded:
            return .green
        case .warning:
            return .yellow
        case .failed:
            return .red
        }
    }

    static func accessibilityLabel(for state: OperationTimelineItem.State) -> String {
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

struct OperationTimelineStateIcon: View {
    let state: OperationTimelineItem.State
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        icon
            .font(.system(size: 16, weight: .semibold))
            .foregroundStyle(OperationTimelineVisualStyle.color(for: state))
            .frame(width: 20, height: 20)
            .scaleEffect(scale)
            .animation(animation, value: state)
            .accessibilityLabel(OperationTimelineVisualStyle.accessibilityLabel(for: state))
    }

    @ViewBuilder
    private var icon: some View {
        switch state {
        case .pending:
            Image(systemName: OperationTimelineVisualStyle.symbol(for: state))
        case .running:
            RotatingTimelineIcon()
        case .succeeded, .warning, .failed:
            Image(systemName: OperationTimelineVisualStyle.symbol(for: state))
        }
    }

    private var scale: CGFloat {
        switch state {
        case .running:
            return 1.05
        case .succeeded:
            return 1.08
        case .pending, .warning, .failed:
            return 1
        }
    }

    private var animation: Animation? {
        reduceMotion ? nil : .snappy(duration: 0.18)
    }
}

private struct RotatingTimelineIcon: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isRotating = false

    var body: some View {
        Image(systemName: OperationTimelineVisualStyle.symbol(for: .running))
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
