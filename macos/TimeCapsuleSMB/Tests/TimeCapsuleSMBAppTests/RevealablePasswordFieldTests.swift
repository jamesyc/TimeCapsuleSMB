import AppKit
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class RevealablePasswordFieldTests: XCTestCase {
    func testToggleRevealsAndConcealsPasswordWithoutChangingValue() throws {
        let model = RevealablePasswordFieldTestModel()
        let host = NSHostingView(rootView: RevealablePasswordFieldTestHarness(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 400, height: 60)
        refresh(host)

        XCTAssertNotNil(secureField(in: host))

        try XCTUnwrap(visibilityButton(in: host)).performClick(nil)
        refresh(host)

        XCTAssertNil(secureField(in: host))
        XCTAssertEqual(editableTextField(in: host)?.stringValue, "secret")
        XCTAssertEqual(model.password, "secret")

        try XCTUnwrap(visibilityButton(in: host)).performClick(nil)
        refresh(host)

        XCTAssertNotNil(secureField(in: host))
        XCTAssertEqual(model.password, "secret")
    }

    func testClearingPasswordConcealsRevealedField() throws {
        let model = RevealablePasswordFieldTestModel()
        let host = NSHostingView(rootView: RevealablePasswordFieldTestHarness(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 400, height: 60)
        refresh(host)

        try XCTUnwrap(visibilityButton(in: host)).performClick(nil)
        refresh(host)
        XCTAssertNil(secureField(in: host))

        model.password = ""
        refresh(host)

        XCTAssertNotNil(secureField(in: host))
    }

    func testSubmitCallbackWorksWhileConcealedAndRevealed() throws {
        let model = RevealablePasswordFieldTestModel()
        let host = NSHostingView(rootView: RevealablePasswordFieldTestHarness(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 400, height: 60)
        refresh(host)

        let concealedField = try XCTUnwrap(editableTextField(in: host))
        let concealedAction = try XCTUnwrap(concealedField.action)
        XCTAssertTrue(NSApp.sendAction(concealedAction, to: concealedField.target, from: concealedField))
        XCTAssertEqual(model.submitCount, 1)

        try XCTUnwrap(visibilityButton(in: host)).performClick(nil)
        refresh(host)

        let revealedField = try XCTUnwrap(editableTextField(in: host))
        let revealedAction = try XCTUnwrap(revealedField.action)
        XCTAssertTrue(NSApp.sendAction(revealedAction, to: revealedField.target, from: revealedField))
        XCTAssertEqual(model.submitCount, 2)
    }

    func testVisibilityLabelsAreLocalizedInEverySupportedLanguage() {
        for language in AppLanguage.allCases where language.localizationIdentifier != nil {
            let show = L10n.string("password.show", language: language)
            let hide = L10n.string("password.hide", language: language)

            XCTAssertNotEqual(show, "password.show", language.rawValue)
            XCTAssertNotEqual(hide, "password.hide", language.rawValue)
            XCTAssertNotEqual(show, hide, language.rawValue)
        }
    }

    private func refresh(_ host: NSView) {
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        host.layoutSubtreeIfNeeded()
        host.displayIfNeeded()
    }

    private func visibilityButton(in view: NSView) -> NSButton? {
        descendants(of: view).compactMap { $0 as? NSButton }.first
    }

    private func secureField(in view: NSView) -> NSSecureTextField? {
        descendants(of: view).compactMap { $0 as? NSSecureTextField }.first
    }

    private func editableTextField(in view: NSView) -> NSTextField? {
        descendants(of: view).compactMap { $0 as? NSTextField }.first(where: \.isEditable)
    }

    private func descendants(of view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants(of: $0) }
    }
}

@MainActor
private final class RevealablePasswordFieldTestModel: ObservableObject {
    @Published var password = "secret"
    var submitCount = 0
}

private struct RevealablePasswordFieldTestHarness: View {
    @ObservedObject var model: RevealablePasswordFieldTestModel

    var body: some View {
        RevealablePasswordField("Password", text: $model.password) {
            model.submitCount += 1
        }
    }
}
