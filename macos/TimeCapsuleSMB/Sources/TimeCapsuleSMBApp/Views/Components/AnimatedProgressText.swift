import SwiftUI

struct AnimatedProgressText: View {
    let message: String
    let isRunning: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase = 0

    var body: some View {
        Text(animatedMessage)
            .onChange(of: animationIdentity) { _, _ in
                phase = 0
            }
            .task(id: animationIdentity) {
                await animateWhileRunning()
            }
    }

    private var animatedMessage: String {
        ProgressTextAnimator.message(message, isRunning: shouldAnimate, phase: phase) ?? message
    }

    private var animationIdentity: String? {
        ProgressTextAnimator.shouldAnimate(message, isRunning: shouldAnimate) ? message : nil
    }

    private var shouldAnimate: Bool {
        isRunning && !reduceMotion
    }

    private func animateWhileRunning() async {
        phase = 0
        guard animationIdentity != nil else {
            return
        }
        while !Task.isCancelled {
            do {
                try await Task.sleep(nanoseconds: ProgressTextAnimator.frameIntervalNanoseconds)
            } catch {
                return
            }
            guard !Task.isCancelled else {
                return
            }
            phase = ProgressTextAnimator.nextPhase(after: phase)
        }
    }
}
