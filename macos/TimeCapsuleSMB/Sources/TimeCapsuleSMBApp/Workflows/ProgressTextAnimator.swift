import Foundation

enum ProgressTextAnimator {
    static let frameInterval: TimeInterval = 0.3
    static let frameIntervalNanoseconds: UInt64 = UInt64(frameInterval * 1_000_000_000)
    static let frameCount = 3

    static func message(_ message: String?, isRunning: Bool, phase: Int) -> String? {
        guard shouldAnimate(message, isRunning: isRunning),
              let base = animationBase(message) else {
            return message
        }
        return base + String(repeating: ".", count: frameIndex(phase) + 1)
    }

    static func shouldAnimate(_ message: String?, isRunning: Bool) -> Bool {
        guard isRunning,
              let message,
              !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        return true
    }

    static func nextPhase(after phase: Int) -> Int {
        (frameIndex(phase) + 1) % frameCount
    }

    private static func animationBase(_ message: String?) -> String? {
        guard let trimmed = message?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else {
            return nil
        }
        let stripped = trimmed.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return stripped.isEmpty ? nil : stripped
    }

    private static func frameIndex(_ phase: Int) -> Int {
        ((phase % frameCount) + frameCount) % frameCount
    }
}
