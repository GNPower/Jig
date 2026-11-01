"""Reading board files: ``boards/<name>/board.jig`` evaluated into a ``Board``.

A board file holds const lines and one ``board "<name>" { ... }``. Jig
evaluates it with the option values a run chose (each option's default,
then jig.toml's ``[defaults] options``, then the system's board line, then
``--option``), so ``if``/``elif``/``else`` in the board and in device bodies
pick declarations, and expressions and string fields use the options
(``"zed-node-{node}.elf"``). Strings also see ``workdir``, the machine's
directory in the run (``<workdir>`` for ``jig check`` and ``jig platform``).

Declarations come in any order: Jig collects the names of the devices,
peripherals and links first, so a link can name a peripheral declared below
it. A name stands for a reference: ``zynq.uart1`` is a device interface,
``zynq.mio[7]`` a pin, ``flash0`` a peripheral, ``chip.IRQ`` one of its
outputs, ``adc.channel[3]`` an analog input.

A device's family and the layers of a stack are read from the text: in
``device zynq: zynq7000(...)`` the family is ``zynq7000`` whether or not a
plugin that defines it has loaded yet, and in ``interface can0: csp over cfp``
each layer is a name with optional arguments. ``Board`` checks them against
the families and layers Jig knows once the board's plugin has loaded
(``jig.system.socs.driver_for``, ``jig check``).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from jig.errors import ConfigError
from jig.lang.interpreter import Interpreter, RangeValue, Scope
from jig.lang.library import DslError
from jig.lang.parser import PlanError, Program, load_board_file, unquote
from jig.lang.values import Regex, show, truthy
from jig.system import protocols
from jig.system.boards import (
    DEFAULT_MIPS,
    POWER_STATES,
    Board,
    CosimSpec,
    Interface,
    RegisterMapSource,
    SectionContext,
    Sensor,
    Storage,
    default_root,
)
from jig.system.model import (
    RESERVED_OPTIONS,
    AirhdlSource,
    Assignment,
    BootMode,
    ChannelRef,
    ChoiceSpec,
    CosimSource,
    DesignName,
    Device,
    DeviceRef,
    FirmwareBuild,
    FirmwareCommand,
    InterfaceStack,
    InternalRef,
    IpBlock,
    Link,
    LinkRef,
    LinkSpec,
    MemorySpec,
    ModelSpec,
    OptionSpec,
    Peripheral,
    PeripheralInput,
    PeripheralOutput,
    PeripheralRef,
    PinRef,
    ProtocolInstance,
    SensorSpec,
    Unmodelled,
)
from jig.system.protocols import Layer

PLACEHOLDER_WORKDIR = "<workdir>"
"""What ``workdir`` holds when no run builds the machine (``jig check``,
``jig platform``)."""

BOARD_SETTINGS = (
    "description",
    "root",
    "models",
    "plugin",
    "rtc",
    "storage",
    "backup_supply",
    "power",
    "repl",
)
DEVICE_SETTINGS = ("firmware", "driver", "commands")
LINK_KINDS = ("uart", "can", "spi", "qspi", "i2c", "gpio")
BUS_KINDS = ("spi", "qspi", "i2c")
_GLOB = frozenset("*?[")
_FRAME = re.compile(r"[5-9][NOEMS][12]")


class _Clock:
    """What the evaluator's interpreter asks of a simulation: no time passes."""

    now = 0.0
    now_ns = 0
    scheduler = None


class _DesignScope(Scope):
    """An ``ip`` line's scope: a name nothing declares is a port or an
    instance of the block design (``DesignName``)."""

    __slots__ = ()

    def lookup(self, name: str) -> Any:
        if self.find(name) is None:
            return DesignName(name)
        return super().lookup(name)


class _Evaluator(Interpreter):
    """The plan interpreter's expressions, without a simulation, plus a model
    in a board file being a string with properties (``"Model" (size: 4)``)."""

    def __init__(self, program: Program) -> None:
        super().__init__(program, _Clock())  # type: ignore[arg-type]

    async def _call(
        self, function: Any, positional: list[Any], named: dict[str, Any], scope: Scope
    ) -> Any:
        if isinstance(function, str):
            if positional:
                raise PlanError(
                    f'a model takes its properties by name: "{function}" (size: 32 MiB)'
                )
            return ModelSpec(function, dict(named))
        return await super()._call(function, positional, named, scope)


class BoardFile:
    """A board file, parsed: ``evaluate`` turns it into a ``Board`` for one
    set of option values."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.directory = self.path.parent
        try:
            self.program = load_board_file(self.path)
        except PlanError as exc:
            raise ConfigError(self._located(exc.where, exc.reason)) from None
        self.node = self.program.main.items("BoardDefinition")[0]
        self.name = unquote(self.node.name.value)

    def __repr__(self) -> str:
        return f"<board file {self.path}>"

    @property
    def label(self) -> str:
        """``<board directory>/board.jig``, for messages."""
        return f"{self.directory.name}/{self.path.name}"

    def _located(self, where: str, message: str) -> str:
        if where.startswith(self.path.name + ":"):
            where = f"{self.directory.name}/{where}"
        return f"{where}: {message}" if where else f"{self.label}: {message}"

    def where(self, node: Any) -> str:
        line, column = self.program.main.model._tx_parser.pos_to_linecol(node._tx_position)
        return f"{self.label}:{line}:{column}"

    @functools.cached_property
    def options(self) -> dict[str, OptionSpec]:
        """The options the board offers, with their choices and defaults."""
        evaluator = self._evaluator()
        scope = Scope(evaluator.globals)
        _declare_option_constructors(scope)
        options: dict[str, OptionSpec] = {}
        for member in self.node.members:
            if type(member).__name__ != "OptionDecl":
                continue
            where = self.where(member)
            name = member.name
            if name in RESERVED_OPTIONS:
                raise ConfigError(
                    f"{where}: {name} is a setting every board takes from a system, "
                    "so no option can use the name"
                )
            if name in options:
                raise ConfigError(f"{where}: option {name} is declared twice")
            value = self._value(evaluator, member.value, scope, where)
            if isinstance(value, ChoiceSpec):
                options[name] = OptionSpec(name, value.default, dict(value.descriptions), where)
            elif value is None or isinstance(value, (list, dict, ModelSpec, LinkSpec)):
                raise ConfigError(
                    f"{where}: an option is choice(...) or a plain value, not {show(value)}"
                )
            else:
                options[name] = OptionSpec(name, value, None, where)
        return options

    def _evaluator(self) -> _Evaluator:
        try:
            return _Evaluator(self.program)
        except (PlanError, ConfigError) as exc:
            reason = exc.reason if isinstance(exc, PlanError) else str(exc)
            where = exc.where if isinstance(exc, PlanError) else ""
            raise ConfigError(self._located(where, reason)) from None

    def _value(self, evaluator: _Evaluator, node: Any, scope: Scope, where: str) -> Any:
        try:
            return evaluator._evaluate_now(node, scope)
        except PlanError as exc:
            raise ConfigError(f"{where}: {exc.reason}") from None
        except (DslError, ConfigError) as exc:
            raise ConfigError(f"{where}: {exc}") from None

    # ------------------------------------------------------------ evaluation

    def evaluate(
        self, options: Mapping[str, Any] | None = None, workdir: str | None = None
    ) -> Board:
        """The board for these option values (each option's default
        otherwise) and machine directory (``<workdir>`` otherwise)."""
        return _Evaluation(self, options or {}, workdir or PLACEHOLDER_WORKDIR).board()


class _Evaluation:
    """One evaluation of a board file."""

    def __init__(self, file: BoardFile, options: Mapping[str, Any], workdir: str) -> None:
        self.file = file
        self.evaluator = file._evaluator()
        self.scope = Scope(self.evaluator.globals)
        self.workdir = workdir
        self.root = file.directory
        specs = file.options
        unknown = sorted(set(options) - set(specs))
        if unknown:
            known = ", ".join(specs) or "none"
            raise ConfigError(
                f"board {file.name} has no option {', '.join(unknown)} (has: {known})"
            )
        where = f"board {file.name}"
        self.option_values = {
            name: spec.accept(options[name], where) if name in options else spec.default
            for name, spec in specs.items()
        }
        self.scope.declare("workdir", workdir)
        for name, value in self.option_values.items():
            self.scope.declare(name, value)

    def where(self, node: Any) -> str:
        return self.file.where(node)

    def value(self, node: Any, scope: Scope | None = None, *, where: str | None = None) -> Any:
        return self.file._value(
            self.evaluator, node, scope or self.scope, where or self.where(node)
        )

    # ------------------------------------------------------------- selection

    def active(self, members: list[Any], kind: str) -> Iterator[Any]:
        """The members ``if``/``elif``/``else`` pick, in file order."""
        for member in members:
            name = type(member).__name__
            if name in ("BoardIf", "DeviceIf"):
                chosen = None
                if truthy(self.value(member.condition)):
                    chosen = member.members
                else:
                    for branch in member.elifs:
                        if truthy(self.value(branch.condition)):
                            chosen = branch.members
                            break
                    else:
                        chosen = member.otherwise
                yield from self.active(list(chosen or []), kind)
            elif name == "OptionDecl" and kind == "board":
                continue  # read with the board's options
            else:
                yield member

    def _check_options_are_top_level(self) -> None:
        def walk(members: list[Any]) -> None:
            for member in members:
                if type(member).__name__ == "BoardIf":
                    branches = [member.members, *(b.members for b in member.elifs)]
                    for branch in [*branches, member.otherwise or []]:
                        for inner in branch:
                            if type(inner).__name__ == "OptionDecl":
                                raise ConfigError(
                                    f"{self.where(inner)}: options are declared at the "
                                    "board's top level"
                                )
                        walk(list(branch))

        walk(list(self.file.node.members))

    # ------------------------------------------------------------------ board

    def board(self) -> Board:
        self._check_options_are_top_level()
        members = list(self.active(list(self.file.node.members), "board"))
        devices = [m for m in members if type(m).__name__ == "DeviceDecl"]
        peripherals = [m for m in members if type(m).__name__ == "PeripheralDecl"]
        links = [m for m in members if type(m).__name__ == "LinkDecl"]
        sensors = [m for m in members if type(m).__name__ == "SensorDecl"]
        settings = [m for m in members if type(m).__name__ == "Setting"]
        self._declare_names(devices, peripherals, links, sensors)
        values = self._settings(settings)
        self._declare_constructors()
        if len(devices) > 1:
            listed = ", ".join(d.name for d in devices)
            raise ConfigError(
                f"{self.where(devices[1])}: board {self.file.name} declares {len(devices)} "
                f"devices ({listed}); boards with several devices come in a later release "
                "of Jig (notation step 5)"
            )
        if not devices:
            raise ConfigError(f"{self.file.label}: board {self.file.name} declares no device")
        device = self._device(devices[0])
        parts = {p.name: self._peripheral(p) for p in peripherals}
        board_links = [self._link(link) for link in links]
        parts = self._wire(device, parts, board_links)
        sensor_specs = [self._sensor(s, parts, device) for s in sensors]
        return self._compat(values, device, parts, board_links, sensor_specs)

    def _declare_names(
        self, devices: list[Any], peripherals: list[Any], links: list[Any], sensors: list[Any]
    ) -> None:
        taken: dict[str, str] = {}
        reserved = {
            "workdir": "the machine's directory",
            **{name: "an option" for name in self.option_values},
            **{name: "a constant" for name in self.evaluator.globals.vars},
            **{name: "a constructor" for name in _CONSTRUCTOR_NAMES},
        }
        for kind, items in (
            ("device", devices),
            ("peripheral", peripherals),
            ("link", links),
            ("sensor", sensors),
        ):
            for item in items:
                where = self.where(item)
                name = item.name
                if name in reserved:
                    raise ConfigError(f"{where}: {kind} {name}: {name} is {reserved[name]}")
                if name in taken:
                    raise ConfigError(
                        f"{where}: {name} is declared twice ({taken[name]} and {kind})"
                    )
                taken[name] = kind
                if kind == "device":
                    self.scope.declare(name, DeviceRef(name))
                elif kind == "peripheral":
                    self.scope.declare(name, PeripheralRef(name))
                elif kind == "link":
                    self.scope.declare(name, LinkRef(name))

    def _declare_constructors(self) -> None:
        constructors: dict[str, Callable[..., Any]] = {
            "memory": _memory,
            "cosim": self._cosim,
            "airhdl": self._airhdl,
            "build": self._build,
            "command": _command,
        }
        _declare_option_constructors(self.scope)
        for name, function in constructors.items():
            self.scope.declare(name, function)

    # --------------------------------------------------------------- settings

    def _settings(self, settings: list[Any]) -> dict[str, Any]:
        from jig.system import registry

        sections = registry.extension_sections()
        values: dict[str, Any] = {}
        nodes: dict[str, Any] = {}
        root_node = next((s for s in settings if s.name == "root"), None)
        if root_node is not None:
            settings = [root_node, *(s for s in settings if s is not root_node)]
        for setting in settings:
            where = self.where(setting)
            name = setting.name
            if name in nodes:
                raise ConfigError(f"{where}: {name} is set twice")
            if name not in BOARD_SETTINGS and name not in sections:
                known = ", ".join([*BOARD_SETTINGS, *sections])
                raise ConfigError(f"{where}: board setting {name} is not one of {known}")
            nodes[name] = setting
            value = self.value(setting.value)
            if name in sections:
                context = SectionContext(
                    self.file.name, f"{where}: {name}", self.file.directory, self.root
                )
                values[name] = sections[name].value(value, context)
                continue
            values[name] = self._setting(name, value, where)
            if name == "root":
                self.root = values[name]
        if "root" not in values:
            self.root = default_root(self.file.directory)
        values.setdefault("root", self.root)
        return values

    def _setting(self, name: str, value: Any, where: str) -> Any:
        directory = self.file.directory
        if name == "description":
            return _text(value, f"{where}: description")
        if name == "root":
            root = (directory / _text(value, f"{where}: root")).resolve()
            if not root.is_dir():
                raise ConfigError(f"{where}: root {root} is not a directory")
            return root
        if name == "models":
            return _models(value, directory, where)
        if name == "plugin":
            plugin = (directory / _text(value, f"{where}: plugin")).resolve()
            if not plugin.exists():
                raise ConfigError(f"{where}: plugin {plugin} not found")
            return plugin
        if name == "rtc":
            if not isinstance(value, (PeripheralRef, InternalRef)):
                raise ConfigError(
                    f"{where}: rtc is a peripheral or a device's own clock (mcu.rtc), "
                    f"not {show(value)}"
                )
            return value
        if name in ("storage", "backup_supply"):
            items = value if isinstance(value, list) else None
            if items is None or not all(isinstance(item, PeripheralRef) for item in items):
                raise ConfigError(f"{where}: {name} is a list of peripherals, such as [flash0]")
            return items
        if name == "power":
            if value not in POWER_STATES:
                raise ConfigError(f'{where}: power is "on" or "off", not {show(value)}')
            return value
        # repl
        files = value if isinstance(value, list) else [value]
        paths = []
        for item in files:
            path = (directory / _text(item, f"{where}: repl")).resolve()
            if not path.is_file():
                raise ConfigError(f"{where}: repl fragment {path} not found")
            paths.append(path)
        return tuple(paths)

    # ---------------------------------------------------------------- devices

    def _device(self, node: Any) -> Device:
        where = self.where(node)
        form = _call_form(node.value)
        if form is None:
            raise ConfigError(
                f"{where}: a device is declared as FAMILY(arguments), such as "
                'zynq7000(part: "XC7Z020-CLG484") or stm32(part: "STM32F407VG")'
            )
        family, call = form
        arguments = self._arguments(call, where, family)
        boot: list[BootMode] = []
        protocols_: dict[str, ProtocolInstance] = {}
        stacks: dict[str, InterfaceStack] = {}
        ips: list[IpBlock] = []
        unmodelled: list[Unmodelled] = []
        assignments: list[Assignment] = []
        settings: dict[str, Any] = {}
        members = list(self.active(list(node.members or []), "device"))
        for member in members:  # protocol instances first: stacks name them
            if type(member).__name__ == "ProtocolDecl":
                instance = self._protocol(member)
                if instance.name in protocols_:
                    raise ConfigError(
                        f"{instance.where}: device {node.name} declares protocol "
                        f"{instance.name} twice; name one: "
                        f"protocol NAME: {instance.layer.name}(...)"
                    )
                protocols_[instance.name] = instance
        for member in members:
            kind = type(member).__name__
            member_where = self.where(member)
            if kind == "BootDecl":
                if any(mode.name == member.mode for mode in boot):
                    raise ConfigError(f"{member_where}: boot {member.mode} is listed twice")
                value = self.value(member.settings) if member.settings is not None else {}
                if not isinstance(value, dict):
                    raise ConfigError(
                        f"{member_where}: boot {member.mode} takes a map of settings, "
                        f"not {show(value)}"
                    )
                boot.append(BootMode(member.mode, _plain(value)))
            elif kind == "InterfaceDecl":
                if member.name in stacks:
                    raise ConfigError(f"{member_where}: interface {member.name} is declared twice")
                stacks[member.name] = self._stack(member, protocols_)
            elif kind == "IpDecl":
                ips.append(self._ip(member, node.name))
            elif kind == "UnmodelledDecl":
                unmodelled.append(self._unmodelled(member))
            elif kind == "Assign":
                assignments.append(self._assignment(member))
            elif kind == "Setting":
                if member.name not in DEVICE_SETTINGS:
                    raise ConfigError(
                        f"{member_where}: device setting {member.name} is not one of "
                        f"{', '.join(DEVICE_SETTINGS)}"
                    )
                if member.name in settings:
                    raise ConfigError(f"{member_where}: {member.name} is set twice")
                settings[member.name] = (self.value(member.value), member_where)
        names = [ip.name for ip in ips]
        for ip in ips:
            if names.count(ip.name) > 1:
                raise ConfigError(f"{ip.where}: ip {ip.name} is declared twice")
        firmware = settings.get("firmware", (None, ""))[0]
        driver = settings.get("driver", (None, ""))[0]
        if driver is not None and (not isinstance(driver, str) or ":" not in driver):
            raise ConfigError(
                f"{settings['driver'][1]}: driver is 'module:Class' or 'file.py:Class'"
            )
        commands = settings.get("commands", ([], ""))[0]
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            raise ConfigError(f"{settings['commands'][1]}: commands is a list of monitor commands")
        return Device(
            name=node.name,
            family=family,
            arguments=arguments,
            where=where,
            firmware=firmware,
            boot=tuple(boot),
            protocols=tuple(protocols_.values()),
            interfaces=tuple(stacks.values()),
            ips=tuple(ips),
            unmodelled=tuple(unmodelled),
            assignments=tuple(assignments),
            commands=tuple(commands),
            driver=driver,
        )

    def _arguments(self, call: Any, where: str, what: str) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        for argument in call.args if call is not None else []:
            if argument.name is None:
                raise ConfigError(f"{where}: {what}() takes its arguments by name")
            if argument.name in arguments:
                raise ConfigError(f"{where}: {what}(): {argument.name} is given twice")
            arguments[argument.name] = self.value(argument.value, where=where)
        return arguments

    def _layer(self, node: Any, where: str) -> Layer:
        form = _call_form(node)
        if form is None:
            raise ConfigError(
                f"{where}: a protocol layer is a name or a name with arguments, such as "
                'kiss or cli(eol: "\\r\\n")'
            )
        name, call = form
        arguments = self._arguments(call, where, name)
        kind = protocols.known().get(name)
        settings = kind.settings(arguments, f"{where}: {name}") if kind is not None else None
        return Layer(name, arguments, settings)

    def _protocol(self, node: Any) -> ProtocolInstance:
        where = self.where(node)
        layer = self._layer(node.value, where)
        return ProtocolInstance(node.name or layer.name, layer, where)

    def _stack(self, node: Any, instances: Mapping[str, ProtocolInstance]) -> InterfaceStack:
        where = self.where(node)
        stack = node.stack
        operands = stack.layers if type(stack).__name__ == "Stack" else [stack]
        layers: list[Layer] = []
        for operand in operands:
            form = _call_form(operand)
            if form is not None and form[0] in instances:
                name, call = form
                if call is not None:
                    raise ConfigError(
                        f"{where}: {name} is the device's protocol line; a stack names it "
                        "without arguments"
                    )
                instance = instances[name]
                layers.append(
                    Layer(
                        instance.layer.name, instance.layer.arguments, instance.layer.settings, name
                    )
                )
            else:
                layers.append(self._layer(operand, where))
        return InterfaceStack(node.name, tuple(layers), where)

    def _ip(self, node: Any, device: str) -> IpBlock:
        where = self.where(node)
        value = self.value(node.value, _DesignScope(self.scope), where=where)
        cosim = None
        memory = False
        if isinstance(value, str):
            value = ModelSpec(value)
        if isinstance(value, ModelSpec):
            model, properties = value.model, dict(value.properties)
        elif isinstance(value, MemorySpec):
            model, properties, memory = "Memory.MappedMemory", dict(value.properties), True
        elif isinstance(value, CosimSource):
            model, properties, cosim = (
                "CoSimulated.CoSimulatedPeripheral",
                dict(value.properties),
                value,
            )
        else:
            raise ConfigError(
                f'{where}: an ip line names a model ("Vendor.Model" (...)), memory(...) or '
                f"cosim(...), not {show(value)}"
            )
        on = _placement(properties.pop("on", None), where, device)
        irq = _design_name(properties.pop("irq", None), device)
        if isinstance(irq, DesignName):
            irq = irq.name
        elif irq is not None and (
            not isinstance(irq, int) or isinstance(irq, bool) or not 0 <= irq < 16
        ):
            raise ConfigError(
                f"{where}: irq is an exported interrupt port or an IRQ_F2P index 0-15, "
                f"not {show(irq)}"
            )
        for key, item in list(properties.items()):
            item = properties[key] = _design_name(item, device)
            if isinstance(item, (DeviceRef, LinkRef, PinRef, PeripheralOutput, PeripheralInput)):
                raise ConfigError(
                    f"{where}: ip {node.name}: {key} names {item!r}, which no model property "
                    "takes; a property can name a peripheral or an instance of the block design"
                )
        if memory:
            _memory_size(properties, on, where)
        return IpBlock(node.name, model, properties, on, irq, where, memory, cosim)

    def _unmodelled(self, node: Any) -> Unmodelled:
        where = self.where(node)
        value = self.value(node.range)
        if not isinstance(value, RangeValue) or not all(
            isinstance(end, int) and not isinstance(end, bool) for end in (value.low, value.high)
        ):
            raise ConfigError(f"{where}: unmodelled takes an address range LOW .. HIGH")
        if value.high < value.low:
            raise ConfigError(f"{where}: the range ends below where it starts")
        return Unmodelled(node.name, int(value.low), int(value.high), where)

    def _assignment(self, node: Any) -> Assignment:
        where = self.where(node)
        target = node.target
        ops = list(target.ops)
        if (
            node.op != "="
            or type(target.base).__name__ != "Name"
            or len(ops) != 1
            or type(ops[0]).__name__ != "Attribute"
        ):
            raise ConfigError(
                f"{where}: a device sets a property of one of its own peripherals as "
                "PERIPHERAL.PROPERTY = VALUE, such as fdcan1.kernelFrequency = 16 MHz"
            )
        return Assignment(target.base.name, ops[0].name, self.value(node.value), where)

    # ------------------------------------------------------------ peripherals

    def _peripheral(self, node: Any) -> Peripheral:
        where = self.where(node)
        value = self.value(node.value)
        if isinstance(value, str):
            value = ModelSpec(value)
        if not isinstance(value, ModelSpec):
            raise ConfigError(
                f'{where}: a peripheral names its model, such as "SPI.Micron_MT25Q" '
                f"(size: 32 MiB), not {show(value)}"
            )
        return Peripheral(node.name, value.model, dict(value.properties), where)

    # ------------------------------------------------------------------ links

    def _link(self, node: Any) -> Link:
        where = self.where(node)
        form = _call_form(node.value)
        if form is None or form[0] not in LINK_KINDS or form[1] is None:
            raise ConfigError(
                f"{where}: a link is declared as KIND(...), with KIND one of "
                f"{', '.join(LINK_KINDS)}"
            )
        kind, call = form
        try:
            spec = _link_constructor(kind)(**self._arguments(call, where, kind))
        except DslError as exc:
            raise ConfigError(f"{where}: {exc}") from None
        settings = dict(spec.settings)
        name = node.name
        if spec.kind in ("uart", "can"):
            members = settings.pop("members", None)
            if not isinstance(members, list) or not members:
                raise ConfigError(f"{where}: link {name}: members is a list of device interfaces")
            for member in members:
                if not isinstance(member, InternalRef):
                    raise ConfigError(
                        f"{where}: link {name}: members are device interfaces such as "
                        f"zynq.uart0, not {show(member)}"
                    )
            if spec.kind == "uart" and len(members) > 2:
                raise ConfigError(f"{where}: link {name}: a UART joins two interfaces at most")
            _rate(settings, "baud" if spec.kind == "uart" else "bitrate", where)
            if spec.kind == "uart":
                frame = settings.setdefault("frame", "8N1")
                if not isinstance(frame, str) or not _FRAME.fullmatch(frame):
                    raise ConfigError(
                        f"{where}: link {name}: frame is data bits, parity and stop bits, "
                        'such as "8N1"'
                    )
            return Link(name, spec.kind, node.external, where, settings, tuple(members))
        if spec.kind in BUS_KINDS:
            controller = settings.pop("controller", None)
            targets = settings.pop("targets", None)
            if not isinstance(controller, InternalRef):
                raise ConfigError(
                    f"{where}: link {name}: controller is a device interface such as zynq.spi0"
                )
            if not isinstance(targets, dict) or not targets:
                raise ConfigError(
                    f"{where}: link {name}: targets is a map from address to peripheral, "
                    "such as {0: flash0}"
                )
            chosen: dict[int, str] = {}
            for address, target in targets.items():
                if isinstance(target, (InternalRef, DeviceRef)):
                    raise ConfigError(
                        f"{where}: link {name}: {target!r} is a device, and a device can't be "
                        f"an {spec.kind.upper()} target: Renode returns a target's reply "
                        "inside the controller's own call, so a target on another machine "
                        "would need new models on both ends"
                    )
                if not isinstance(address, int) or isinstance(address, bool) or address < 0:
                    raise ConfigError(
                        f"{where}: link {name}: {show(address)} is not a chip select or address"
                    )
                if spec.kind == "i2c" and address > 0x7F:
                    raise ConfigError(f"{where}: link {name}: 0x{address:X} is not a 7-bit address")
                if not isinstance(target, PeripheralRef):
                    raise ConfigError(
                        f"{where}: link {name}: targets are peripherals, not {show(target)}"
                    )
                chosen[address] = target.peripheral
            if settings:
                raise ConfigError(
                    f"{where}: link {name}: {spec.kind}() takes controller and targets, "
                    f"not {', '.join(settings)}"
                )
            return Link(
                name, spec.kind, node.external, where, {}, controller=controller, targets=chosen
            )
        # gpio
        source = settings.pop("from", None)
        sinks = settings.pop("to", None)
        pull = settings.pop("pull", None)
        if settings:
            raise ConfigError(f"{where}: link {name}: gpio() takes from, to and pull")
        if not isinstance(source, (PeripheralOutput, PinRef, InternalRef)):
            raise ConfigError(
                f"{where}: link {name}: from is one output, such as chip.IRQ or zynq.mio[7]"
            )
        sinks = sinks if isinstance(sinks, list) else [sinks]
        for sink in sinks:
            if not isinstance(sink, (PeripheralInput, PinRef, InternalRef)):
                raise ConfigError(
                    f"{where}: link {name}: to is an input or a list of inputs, such as "
                    f"chip.gpio[0] or zynq.mio[7], not {show(sink)}"
                )
        if pull not in (None, "up", "down"):
            raise ConfigError(f'{where}: link {name}: pull is "up" or "down"')
        return Link(
            name,
            "gpio",
            node.external,
            where,
            {"pull": pull} if pull else {},
            source=source,
            sinks=tuple(sinks),
        )

    def _wire(
        self, device: Device, parts: dict[str, Peripheral], links: list[Link]
    ) -> dict[str, Peripheral]:
        """Each peripheral at the controller its bus link names; every
        interface and pin on one link at most; every peripheral on some link."""
        used: dict[str, str] = {}

        def claim(end: Any, link: Link) -> None:
            key = repr(end)
            if key in used and used[key] != link.name:
                raise ConfigError(
                    f"{link.where}: {key} is on link {used[key]} already: an interface or a "
                    "pin belongs to one link at most"
                )
            used[key] = link.name

        wired: dict[str, Peripheral] = dict(parts)
        named: set[str] = set()
        for link in links:
            for member in link.members:
                self._known_device(member.device, device, link.where)
                claim(member, link)
            if link.controller is not None:
                self._known_device(link.controller.device, device, link.where)
                claim(link.controller, link)
                for address, target in link.targets.items():
                    part = wired[target]
                    if part.bus is not None:
                        raise ConfigError(
                            f"{link.where}: peripheral {target} is on link "
                            f"{used.get('bus:' + target)} already"
                        )
                    used["bus:" + target] = link.name
                    wired[target] = _replace(
                        part, bus=(link.controller.device, link.controller.name, address)
                    )
                    named.add(target)
            if link.kind == "gpio":
                for end in (link.source, *link.sinks):
                    claim(end, link)
                    owner = getattr(end, "peripheral", None)
                    if owner is not None:
                        named.add(owner)
                    elif isinstance(end, (PinRef, InternalRef)):
                        self._known_device(end.device, device, link.where)
        for name, part in wired.items():
            if name not in named:
                raise ConfigError(
                    f"{part.where}: peripheral {name} is on no link: a link wires it to a device"
                )
        return wired

    def _known_device(self, name: str, device: Device, where: str) -> None:
        if name != device.name:
            raise ConfigError(f"{where}: {name} is not a device of the board")

    # ---------------------------------------------------------------- sensors

    def _sensor(self, node: Any, parts: Mapping[str, Peripheral], device: Device) -> SensorSpec:
        where = self.where(node)
        channel = self.value(node.pin)
        if not isinstance(channel, ChannelRef):
            raise ConfigError(
                f"{where}: sensor {node.name} is an analog input, such as adc.channel[3] "
                "or mcu.adc4.channel[13]"
            )
        if isinstance(channel.owner, InternalRef):
            self._known_device(channel.owner.device, device, where)
        initial = None
        if node.initial is not None:
            initial = self.value(node.initial)
            if not isinstance(initial, (int, float)) or isinstance(initial, bool):
                raise ConfigError(
                    f"{where}: sensor {node.name} starts at a voltage, such as 736 mV"
                )
            initial = float(initial)
        return SensorSpec(node.name, channel, initial, where)

    # ------------------------------------------------------------ constructors

    def _path(self, value: Any, base: Path, what: str) -> Path:
        return (base / _text(value, what)).resolve()

    def _airhdl(self, package: Any, module: Any = None) -> AirhdlSource:
        return AirhdlSource(
            self._path(package, self.root, "airhdl package"),
            None if module is None else self._path(module, self.root, "airhdl module"),
        )

    def _cosim(self, **arguments: Any) -> CosimSource:
        for key in ("build", "sources", "library"):
            if key not in arguments:
                raise DslError(f"cosim() needs {key}")
        build = self._path(arguments.pop("build"), self.root, "cosim build")
        sources = self._path(arguments.pop("sources"), self.root, "cosim sources")
        for path in (build, sources):
            if not path.is_file():
                raise DslError(f"cosim(): {path} not found")
        inputs = arguments.pop("inputs", [])
        if not isinstance(inputs, list):
            raise DslError("cosim(): inputs is a list of paths")
        context = arguments.pop("context", None)
        return CosimSource(
            build=build,
            sources=sources,
            inputs=tuple(self._path(item, self.root, "cosim input") for item in inputs),
            library=_text(arguments.pop("library"), "cosim library"),
            context="" if context is None else _text(context, "cosim context"),
            properties=arguments,
        )

    def _build(self, script: Any, sources: Any = None, output: Any = None) -> FirmwareBuild:
        if sources is None or output is None:
            raise DslError("build(script, sources: ..., output: ...) needs sources and output")
        directory = self.file.directory
        return FirmwareBuild(
            self._path(script, directory, "build script"),
            self._path(sources, directory, "build sources"),
            _text(output, "build output"),
        )

    # ------------------------------------------------------- the board record

    def _compat(
        self,
        values: Mapping[str, Any],
        device: Device,
        parts: Mapping[str, Peripheral],
        links: list[Link],
        sensors: list[SensorSpec],
    ) -> Board:
        file = self.file
        root: Path = values["root"]
        family_arguments = self._family_arguments(device, values)
        storage = []
        for ref in values.get("storage", []):
            part = parts[ref.peripheral]
            size = part.properties.get("size")
            storage.append(
                Storage(
                    part.name,
                    part.path,
                    size if isinstance(size, int) and not isinstance(size, bool) else None,
                )
            )
        backup = tuple(ref.peripheral for ref in values.get("backup_supply", []))
        rtc_ref = values.get("rtc")
        rtc = None
        if isinstance(rtc_ref, PeripheralRef):
            rtc = parts[rtc_ref.peripheral].path
        elif isinstance(rtc_ref, InternalRef):
            rtc = rtc_ref.name
        interfaces = [
            _interface(link, device)
            for link in links
            if link.external and link.kind in ("uart", "can")
        ]
        boot: dict[str, Any] = {
            "default": device.boot[0].name if device.boot else _family_default_boot(device.family)
        }
        for mode in device.boot:
            boot[mode.name] = dict(mode.settings)
        sections = {name: value for name, value in values.items() if name not in BOARD_SETTINGS}
        mips = family_arguments.get("mips", DEFAULT_MIPS)
        return Board(
            name=file.name,
            description=str(values.get("description", "")),
            soc=device.family,
            directory=file.directory,
            performance_mips=int(mips),
            soc_settings=dict(family_arguments),
            register_maps=_register_maps(device, parts),
            firmware=_firmware(device.firmware, root, device),
            boot=boot,
            storage=tuple(storage),
            interfaces=tuple(interfaces),
            source=file.path,
            root=root,
            soc_driver=device.driver,
            sensors=tuple(_sensor_record(s, parts) for s in sensors),
            power_restart=tuple(p.path for p in parts.values() if p.name not in backup),
            power_initial=str(values.get("power", "on")),
            models=tuple(values.get("models", ())),
            plugin=values.get("plugin"),
            options=dict(file.options),
            rtc=rtc,
            sections=sections,
            devices=(_with_arguments(device, family_arguments),),
            peripherals=tuple(parts.values()),
            links=tuple(links),
            sensor_specs=tuple(sensors),
            backup_supply=backup,
            repl=tuple(values.get("repl", ())),
            option_values=dict(self.option_values),
            workdir=self.workdir,
            cosim=tuple(_cosim_specs(device, root)),
            file=file,
        )

    def _family_arguments(self, device: Device, values: Mapping[str, Any]) -> dict[str, Any]:
        """The device's arguments as its family reads them, when Jig knows
        the family without the board's plugin (built in or an entry point)."""
        from jig.system import socs

        family = socs.known_family(device.family)
        if family is None:
            if device.driver is None and values.get("plugin") is None:
                known = ", ".join(socs.families())
                raise ConfigError(
                    f"{device.where}: no device family {device.family!r} (known: {known})"
                )
            return dict(device.arguments)
        check = getattr(family, "arguments", None)
        if check is None:
            return dict(device.arguments)
        checked: dict[str, Any] = check(device.arguments, device.where)
        return checked


# ------------------------------------------------------------------ helpers


_CONSTRUCTOR_NAMES = ("choice", "memory", "cosim", "airhdl", "build", "command")


def _declare_option_constructors(scope: Scope) -> None:
    scope.declare("choice", _choice)


_NOT_GIVEN = object()


def _choice(*values: Any, default: Any = _NOT_GIVEN) -> ChoiceSpec:
    """``choice(1, 2)``, ``choice({model: "...", rtl: "..."})``."""
    descriptions: dict[Any, str] = {}
    if len(values) == 1 and isinstance(values[0], dict):
        for key, text in values[0].items():
            if not isinstance(text, str):
                raise DslError(f"choice(): the description of {key!r} is text, not {show(text)}")
            descriptions[key] = text
        ordered = tuple(descriptions)
    else:
        ordered = tuple(values)
        descriptions = {value: "" for value in ordered}
    if not ordered:
        raise DslError("choice() lists the values a system may pick")
    for value in ordered:
        if not isinstance(value, (bool, int, float, str)):
            raise DslError(f"choice(): a value is a number, text or true/false, not {show(value)}")
    if len(set(ordered)) != len(ordered):
        raise DslError("choice(): a value is listed twice")
    chosen = ordered[0] if default is _NOT_GIVEN else default
    if chosen not in ordered:
        raise DslError(f"choice(): the default {show(chosen)} is not one of the values")
    return ChoiceSpec(ordered, descriptions, chosen)


def _link_constructor(kind: str) -> Callable[..., LinkSpec]:
    allowed = {
        "uart": ("baud", "frame", "members"),
        "can": ("bitrate", "members"),
        "spi": ("controller", "targets"),
        "qspi": ("controller", "targets"),
        "i2c": ("controller", "targets"),
        "gpio": ("from", "to", "pull"),
    }[kind]

    def make(**arguments: Any) -> LinkSpec:
        unknown = sorted(set(arguments) - set(allowed))
        if unknown:
            raise DslError(f"{kind}() takes {', '.join(allowed)}, not {', '.join(unknown)}")
        return LinkSpec(kind, arguments)

    make.__name__ = kind
    return make


def _memory(**properties: Any) -> MemorySpec:
    return MemorySpec(properties)


def _command(arguments: Any, output: Any = None) -> FirmwareCommand:
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(item, str) and item for item in arguments)
    ):
        raise DslError('command() takes a list of arguments, such as ["cmake", "--build", "build"]')
    if output is None:
        raise DslError("command([...], output: ...) needs output, the ELF the command leaves")
    return FirmwareCommand(tuple(arguments), _text(output, "command output"))


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{what}: expected text, got {show(value)}")
    return value


def _rate(settings: dict[str, Any], key: str, where: str) -> None:
    value = settings.get(key)
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}: {key} is a positive number, not {show(value)}")
    if isinstance(value, float) and value.is_integer():
        settings[key] = int(value)


def _call_form(node: Any) -> tuple[str, Any] | None:
    """``NAME`` or ``NAME(arguments)`` as written: (name, the call or None)."""
    while True:
        kind = type(node).__name__
        if kind == "Stack" and len(node.layers) == 1:
            node = node.layers[0]
        elif kind in ("Or", "And", "Sum", "Product") and len(node.operands) == 1:
            node = node.operands[0]
        elif kind == "Not" and not node.negated:
            node = node.operand
        elif kind == "Comparison" and node.op is None:
            node = node.left
        elif kind == "Range" and node.high is None:
            node = node.low
        elif kind == "Unary" and node.sign is None:
            node = node.operand
        else:
            break
    if type(node).__name__ != "Postfix" or type(node.base).__name__ != "Name":
        return None
    ops = list(node.ops)
    if not ops:
        return node.base.name, None
    if len(ops) == 1 and type(ops[0]).__name__ == "Call":
        return node.base.name, ops[0]
    return None


def _design_name(value: Any, device: str) -> Any:
    """``zynq.axi_bram_ctrl_0`` in the device's own ``ip`` line is the
    block-design name ``axi_bram_ctrl_0``; other values stay as they are."""
    if isinstance(value, InternalRef) and value.device == device:
        return DesignName(value.name)
    return value


def _placement(value: Any, where: str, device: str) -> Any:
    """An ip line's ``on``: a port or instance name, a map of such names to
    windows, or an address range as (low, high)."""
    value = _design_name(value, device)
    if value is None:
        return None
    if isinstance(value, DesignName):
        return value.name
    if isinstance(value, RangeValue):
        if not all(
            isinstance(end, int) and not isinstance(end, bool) for end in (value.low, value.high)
        ):
            raise ConfigError(f"{where}: on takes whole-number addresses")
        if value.high < value.low:
            raise ConfigError(f"{where}: the range ends below where it starts")
        return (int(value.low), int(value.high))
    if isinstance(value, dict):
        windows = {}
        for port, size in value.items():
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ConfigError(f"{where}: on: the window of {port} is a size, such as 4 KiB")
            windows[str(port)] = size
        return windows
    raise ConfigError(
        f"{where}: on is a block-design port, a map of ports to windows, or an address "
        f"range LOW .. HIGH, not {show(value)}"
    )


def _memory_size(properties: Mapping[str, Any], on: Any, where: str) -> None:
    """``memory(...)`` takes ``size``, or the size of its address range."""
    size = properties.get("size")
    if size is None:
        if not isinstance(on, tuple):
            raise ConfigError(
                f"{where}: memory() needs a size, such as memory(size: 64 KiB), unless on "
                "gives an address range"
            )
        return
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ConfigError(f"{where}: memory(): size is a number of bytes, such as 64 KiB")
    if isinstance(on, tuple) and size != on[1] - on[0] + 1:
        raise ConfigError(
            f"{where}: memory(): size {size} differs from its range of {on[1] - on[0] + 1} bytes"
        )


def _plain(value: Any) -> Any:
    """A value with the board's references turned into their names."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (PeripheralRef, InternalRef, DeviceRef, LinkRef, PinRef)):
        return repr(value)
    if isinstance(value, Regex):
        return value.source
    return value


def _replace(part: Peripheral, **changes: Any) -> Peripheral:
    import dataclasses

    return dataclasses.replace(part, **changes)


def _models(value: Any, directory: Path, where: str) -> tuple[Path, ...]:
    patterns = value if isinstance(value, list) else None
    if patterns is None or not all(isinstance(p, str) and p for p in patterns):
        raise ConfigError(f'{where}: models is a list of glob patterns, such as ["models/*.cs"]')
    files: set[Path] = set()
    for pattern in patterns:
        parts = Path(pattern).parts
        index = next((i for i, part in enumerate(parts) if _GLOB & set(part)), len(parts))
        base = Path(*parts[:index]) if index else Path()
        rest = str(Path(*parts[index:])) if index < len(parts) else ""
        start = (directory / base).resolve()
        matches = [start] if not rest else sorted(start.glob(rest))
        matches = [path for path in matches if path.is_file()]
        if not matches:
            raise ConfigError(f"{where}: models: {pattern!r} matches no file")
        for path in matches:
            if path.suffix != ".cs":
                raise ConfigError(
                    f"{where}: models: {pattern!r} matches {path.name}, not a .cs file"
                )
            files.add(path.resolve())
    return tuple(sorted(files))


def _interface(link: Link, device: Device) -> Interface:
    member = link.members[0]
    stack = device.stack(member.name)
    settings: dict[str, Any] = {
        key: value for key, value in link.settings.items() if key in ("baud", "frame", "bitrate")
    }
    if stack is not None:
        for layer in stack.layers:
            if layer.name == "cli" and isinstance(layer.settings, dict):
                settings["eol"] = layer.settings["eol"]
                if layer.settings.get("prompt") is not None:
                    settings["prompt"] = layer.settings["prompt"]
    return Interface(
        name=link.name,
        kind=link.kind,
        peripheral=member.name,
        settings=settings,
        peripherals=tuple(m.name for m in link.members),
        device=member.device,
        stack=stack.names if stack is not None else (),
    )


def _family_default_boot(family: str) -> str:
    from jig.system import socs

    driver = socs.known_family(family)
    modes = getattr(driver, "boot_modes", ()) if driver is not None else ()
    return str(modes[0]) if modes else "jtag"


def _with_arguments(device: Device, arguments: dict[str, Any]) -> Device:
    import dataclasses

    return dataclasses.replace(device, arguments=arguments)


def _firmware(value: Any, root: Path, device: Device) -> dict[str, Path]:
    """``Board.firmware``: images by role, root-relative. An ELF path
    alone is the ``app``; a firmware Jig builds has no file yet."""
    if value is None or isinstance(value, (FirmwareBuild, FirmwareCommand)):
        return {}
    if isinstance(value, str):
        return {"app": (root / value).resolve()}
    if isinstance(value, dict) and all(isinstance(v, str) and v for v in value.values()):
        return {str(role): (root / path).resolve() for role, path in value.items()}
    raise ConfigError(
        f"{device.where}: firmware is a path, a map of roles to paths, build(...) or "
        f"command(...), not {show(value)}"
    )


def _register_maps(
    device: Device, parts: Mapping[str, Peripheral]
) -> tuple[RegisterMapSource, ...]:
    """Every ``airhdl(...)`` property value, as the overlay fills it in."""
    found: list[RegisterMapSource] = []
    owners: list[tuple[str, Mapping[str, Any]]] = [
        *((ip.name, ip.properties) for ip in device.ips),
        *((part.path, part.properties) for part in parts.values()),
    ]
    for owner, properties in owners:
        for name, value in properties.items():
            if isinstance(value, AirhdlSource):
                found.append(RegisterMapSource(f"{owner}.{name}", value.package, value.module))
    for assignment in device.assignments:
        if isinstance(assignment.value, AirhdlSource):
            found.append(
                RegisterMapSource(
                    f"{assignment.target}.{assignment.name}",
                    assignment.value.package,
                    assignment.value.module,
                )
            )
    return tuple(found)


def _sensor_record(spec: SensorSpec, parts: Mapping[str, Peripheral]) -> Sensor:
    owner = spec.channel.owner
    adc = parts[owner.peripheral].path if isinstance(owner, PeripheralRef) else owner.name
    return Sensor(spec.name, adc, spec.channel.channel, spec.initial)


def _cosim_specs(device: Device, root: Path) -> Iterator[CosimSpec]:
    for ip in device.ips:
        if ip.cosim is None:
            continue
        frequency = ip.properties.get("frequency")
        yield CosimSpec(
            peripheral=ip.name,
            build=ip.cosim.build,
            sources=ip.cosim.sources,
            inputs=ip.cosim.inputs,
            library=ip.cosim.library,
            context=ip.cosim.context,
            root=root,
            frequency=int(frequency) if isinstance(frequency, (int, float)) else None,
        )
