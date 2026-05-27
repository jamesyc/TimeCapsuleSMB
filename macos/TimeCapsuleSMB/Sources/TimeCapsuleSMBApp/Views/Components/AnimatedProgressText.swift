import SwiftUI

struct AnimatedProgressText: View {
    let message: String
    let isRunning: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase = 0

    private let timer = Timer.publish(
        every: ProgressTextAnimator.frameInterval,
        on: .main,
        in: .common
    ).autoconnect()

    var body: some View {
        Text(animatedMessage)
            .onChange(of: animationIdentity) { _, _ in
                phase = 0
            }
            .onReceive(timer) { _ in
                advanceAnimation()
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

    private func advanceAnimation() {
        guard animationIdentity != nil else {
            if phase != 0 {
                phase = 0
            }
            return
        }
        phase = ProgressTextAnimator.nextPhase(after: phase)
    }
}
