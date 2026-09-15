// Embedded in both SDK module formats by the audited r4 patch.
class NexPolyMacroBoundary extends React.Component {
  constructor(props) { super(props); this.state = { failed: false }; }
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error) { this.props.onError(error); }
  render() { return this.state.failed ? null : this.props.children; }
}
var Editor = function Editor(props) {
  var latest = React.useRef(props);
  latest.current = props;
  var current = React.useRef(null);
  var uiWaiters = React.useRef(new Map());
  var command = React.useRef(0);
  var [mode, setMode] = React.useState({ macro: false, command: 0 });
  var [macroView, setMacroView] = React.useState(null);
  var [macroError, setMacroError] = React.useState(null);
  var [sdkId, setSdkId] = React.useState('');
  React.useLayoutEffect(function () {
    var resolve = uiWaiters.current.get(mode.command);
    if (resolve) { uiWaiters.current.delete(mode.command); resolve(); }
  }, [mode]);
  var createController = React.useCallback(function () {
    var controller = new MacroController({
      deferred: !!latest.current.deferMacromoleculesEditor,
      load: async function (generation) {
        setMacroError(null);
        var module = await nexpolyLoadMacroModule();
        controller.check();
        if (generation !== controller.generation) return;
        setMacroView({ Component: module.default, generation: generation, controller: controller, id: controller.sdk.id });
      },
      show: function (value) {
        return new Promise(function (resolve) {
          var id = ++command.current;
          uiWaiters.current.set(id, resolve);
          setMode({ macro: value, command: id });
        });
      },
      fail: function (error) {
        if (current.current !== controller) return;
        setMacroView(null);
        setMacroError(error);
      }
    });
    current.current = controller;
    return controller;
  }, []);
  var requestMode = function (value) {
    var controller = current.current;
    if (!controller) return;
    controller.requestMode(value).catch(function (error) {
      if (error.name !== 'AbortError' && controller.state !== 'failed') setMacroError(error);
    });
  };
  var toggler = !props.disableMacromoleculesEditor ? React.createElement(ModeControl, {
    toggle: requestMode, isPolymerEditor: mode.macro
  }) : undefined;
  var onMicro = async function (sdk) {
    var controller = sdk.structService.nexpolyMacroController;
    try {
      await sdk.structService.info();
      controller.check();
      if (!latest.current.deferMacromoleculesEditor && !latest.current.disableMacromoleculesEditor) await controller.ensure();
      if (sdk.nexpolyInitialMol) await sdk.setMolecule(sdk.nexpolyInitialMol);
      controller.check();
      if (!controller.notified) { controller.notified = true; latest.current.onInit?.(sdk); }
    } catch (error) {
      if (error.name !== 'AbortError') latest.current.errorHandler?.(String(error));
    }
  };
  var onMacro = function (editor) {
    var controller = macroView.controller;
    controller.initialized(editor, macroView.generation);
  };
  React.useEffect(function () {
    var controller = macroView?.controller;
    var macro = controller?.macro;
    if (!macro || macro.disposed) return;
    var toMacro = function () { requestMode(true); };
    var toMicro = function () { requestMode(false); };
    macro.events.switchToMacromoleculesMode.add(toMacro);
    macro.events.switchToMoleculesMode.add(toMicro);
    return function () {
      macro.events.switchToMacromoleculesMode.remove(toMacro);
      macro.events.switchToMoleculesMode.remove(toMicro);
    };
  }, [macroView, mode]);
  React.useEffect(function () {
    return function () {
      // Actual SDK retirement owns cancellation; StrictMode's discarded effect
      // must not retire a controller subsequently created by the live SDK.
      uiWaiters.current.clear();
      window.isPolymerEditorTurnedOn = false;
    };
  }, []);
  var macro = macroView && React.createElement(NexPolyMacroBoundary, {
    key: macroView.id + ':' + macroView.generation,
    onError: function (error) { macroView.controller.rejectAttempt(error, macroView.generation); }
  }, React.createElement(macroView.Component, {
    ketcherId: macroView.id, togglerComponent: toggler,
    isMacromoleculesEditorTurnedOn: mode.macro, onInit: onMacro
  }));
  return React.createElement(React.Fragment, null,
    React.createElement('div', { className: styles.editorsWrapper, style: { display: mode.macro ? undefined : 'none' } }, macro),
    React.createElement('div', { className: styles.editorsWrapper, style: { display: mode.macro ? 'none' : undefined } },
      React.createElement(MicromoleculesEditor, { ...props, ketcherId: sdkId, onSetKetcherId: setSdkId,
        createMacroController: createController, togglerComponent: toggler, onInit: onMicro })),
    macroError && React.createElement('div', { role: 'alert', className: 'np-ketcher-macro-error' },
      '大分子功能加载失败，当前结构已保留。', React.createElement('button', { type: 'button',
        onClick: function () { current.current.ensure().then(() => requestMode(true)).catch(() => {}); }
      }, '重试')));
};
