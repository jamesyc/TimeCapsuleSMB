import AppKit
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class WrappingHStackTests: XCTestCase {
    func testViewsMoveToANewLineOnlyWhenTheyDoNotFit() {
        // Three 100-point boxes with 8 points between them need 316 points.
        XCTAssertEqual(size(width: 400), CGSize(width: 316, height: 20))
        XCTAssertEqual(size(width: 316), CGSize(width: 316, height: 20))
        XCTAssertEqual(size(width: 315), CGSize(width: 208, height: 48))
        // One box per line once two no longer fit.
        XCTAssertEqual(size(width: 150), CGSize(width: 100, height: 76))
    }

    func testAViewWiderThanTheRowShrinksToIt() {
        XCTAssertEqual(size(width: 90), CGSize(width: 90, height: 76))
    }

    /// The layout's size for three boxes that are 100 points wide when they
    /// can be, and narrower when the row is.
    private func size(width: CGFloat) -> CGSize {
        let boxes = WrappingHStack {
            ForEach(0..<3, id: \.self) { _ in
                Color.gray.frame(minWidth: 0, idealWidth: 100, maxWidth: 100, minHeight: 20, maxHeight: 20)
            }
        }
        return NSHostingController(rootView: boxes).sizeThatFits(in: CGSize(width: width, height: 10_000))
    }
}
