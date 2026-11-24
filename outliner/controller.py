# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Julio Angulo
"""Connect the outliner to IngeTrazo's document, edit contexts and history."""
from .i18n import language, tr
from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QSettings, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QBrush, QIcon, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QInputDialog, QMenu,
                               QTabBar, QTreeWidgetItem)
from core.history import (AssignLayerCommand, CompoundCommand, DeleteGroupCommand,
                          DeleteSectionPlanesCommand, HideCommand, SetActiveSectionCommand)
from core.layers import DEFAULT_LAYER

from .model import (Rename, Reparent, bounds_of, entity_key, index_scene,
                    matching_keys, natural_key, topmost)
from .ui import Panel, ROLE, ROOT_KEY, SECTIONS_KEY, icon


class Controller(QObject):
    def __init__(self, app):
        super().__init__(app.window)
        self.app = app
        self.nodes, self.items = {}, {}
        self.wanted = set()
        self._selection_stamp = None
        self._poll_stamp = None
        self._fingerprint = None
        self._busy = False
        self._force = True
        self._language = None
        self._decorated = {}

    def install(self):
        self.panel = Panel()
        self.tree = self.panel.tree
        self.dock = self.app.add_panel("Tree", self.panel)
        self.menu_action = self.app.add_menu_action("JA Tree…", self.show, "Ctrl+Alt+O",
                                 tip=tr("Organizar grupos, componentes y planos de sección."))
        if self.menu_action is not None:
            self.update_menu_icon()
            self.menu_action.setIconVisibleInMenu(True)
        self.app.add_context_menu(self.viewport_menu)
        self.debounce = QTimer(self.panel)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(40)
        self.debounce.timeout.connect(self.refresh)
        self.timer = QTimer(self.panel)
        self.timer.setInterval(220)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.app.on_document_changed(self.queue_refresh)
        self.tree.itemSelectionChanged.connect(self.tree_selection)
        self.tree.itemChanged.connect(self.item_changed)
        self.tree.itemClicked.connect(self.item_clicked)
        self.tree.itemDoubleClicked.connect(self.double_clicked)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.renameRequested.connect(self.rename)
        self.tree.editRequested.connect(self.edit)
        self.tree.leaveRequested.connect(self.leave)
        self.tree.zoomRequested.connect(self.zoom)
        self.tree.moveRequested.connect(self.move)
        self.panel.search.textChanged.connect(self.filter_changed)
        self.panel.kind.currentIndexChanged.connect(self.filter_changed)
        self.panel.locate.clicked.connect(self.locate)
        self.panel.zoom.clicked.connect(self.zoom)
        self.panel.leave.clicked.connect(self.leave)
        self.panel.expand.triggered.connect(self.tree.expandAll)
        self.panel.collapse.triggered.connect(self.collapse)
        self.panel.sort.toggled.connect(self.option_changed)
        self.panel.tags.toggled.connect(self.option_changed)
        self.panel.reload.triggered.connect(self.filter_changed)
        for code, action in self.panel.language_actions.items():
            action.triggered.connect(lambda _checked=False, code=code: self.set_language(code))
        settings = QSettings()
        self.panel.sort.setChecked(settings.value("outliner/sort", False, type=bool))
        self.panel.tags.setChecked(settings.value("outliner/tags", True, type=bool))
        self.panel.installEventFilter(self)
        self.refresh()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.Resize):
            self.queue_refresh()
        return False

    def update_menu_icon(self):
        key = self.app.window.palette().cacheKey()
        if key == getattr(self, "_icon_palette", None):
            return
        self._icon_palette = key
        background = self.app.window.palette().color(QPalette.ColorRole.Window)
        name = "ja_tree_dark" if background.lightness() < 128 else "ja_tree"
        panel_icon = icon(name)
        self.dock.setWindowTitle("Tree")
        self.dock.setWindowIcon(panel_icon)
        if self.menu_action is not None:
            self.menu_action.setIcon(panel_icon)
        # Qt's dock tabs do not automatically inherit the dock window icon.
        for bar in self.app.window.findChildren(QTabBar):
            for index in range(bar.count()):
                if bar.tabText(index) == self.dock.windowTitle():
                    bar.setTabIcon(index, panel_icon)

    def show(self):
        self.app.show_panel(self.dock)
        self.refresh()

    def message(self, text):
        self.panel.status.setText(str(text))
        self.app.viewport.flash_status(str(text), 5000)

    def selection_stamp(self):
        return (id(self.app.scene), frozenset(id(e) for e in self.app.scene.selection))

    def queue_refresh(self):
        if not self.debounce.isActive():
            self.debounce.start()

    def poll(self):
        self.update_menu_icon()
        scene = self.app.scene
        stamp = (id(scene), id(self.app.viewport.history), scene.version,
                 self.selection_stamp(), tuple(id(n["group"]) for n in scene._edit_stack),
                 scene.show_section_planes, self.panel.palette().cacheKey(), language())
        if stamp != self._poll_stamp:
            self._poll_stamp = stamp
            self.queue_refresh()

    def filter_changed(self, *_):
        self._force = True
        self.refresh()

    def set_language(self, code):
        if code not in ("auto", "es", "en", "fr"):
            return
        QSettings().setValue("outliner/language", code)
        from . import i18n
        i18n.invalidate()
        for value, action in self.panel.language_actions.items():
            action.setChecked(value == code)
        self.refresh()

    def option_changed(self, *_):
        settings = QSettings()
        settings.setValue("outliner/sort", self.panel.sort.isChecked())
        settings.setValue("outliner/tags", self.panel.tags.isChecked())
        self.filter_changed()

    def refresh(self):
        self.update_menu_icon()
        if self._busy:
            return
        if self.tree.state() in (QAbstractItemView.State.EditingState,
                                QAbstractItemView.State.DraggingState):
            self.queue_refresh()
            return
        if language() != self._language:
            self._language = language()
            self.panel.retranslate()
            if self.menu_action is not None:
                self.menu_action.setStatusTip(tr("Organizar grupos, componentes y planos de sección."))
                self.menu_action.setToolTip(tr("Organizar grupos, componentes y planos de sección."))
            self._force = True
        try:
            nodes = index_scene(self.app.scene)
        except ValueError as exc:
            self.message(exc)
            return
        stamp = self.selection_stamp()
        external_selection = stamp != self._selection_stamp
        if external_selection:
            selected = self.app.scene.selection
            self.wanted = {key for key, node in nodes.items() if node.entity in selected}
            self._selection_stamp = stamp
        self.wanted.intersection_update(nodes)
        self.nodes = nodes
        open_keys = {entity_key(n["group"]) for n in self.app.scene._edit_stack}
        fingerprint = (id(self.app.scene), tuple((key, n.parent_key, n.name, n.tag, n.kind)
                       for key, n in nodes.items()))
        if self._force or fingerprint != self._fingerprint:
            self._rebuild(open_keys)
            self._fingerprint, self._force = fingerprint, False
        # Update status in place: resetting rows between the two clicks of
        # a double click invalidates Qt's pressed index and loses the edit.
        blocker = QSignalBlocker(self.tree)
        for key, item in self.items.items():
            self.decorate(item, nodes[key], open_keys)
            if key in open_keys:
                item.setExpanded(True)
        self.tree.setColumnHidden(1, not self.panel.tags.isChecked() or self.panel.width() < 330)
        del blocker
        self._sync_rows(reveal=external_selection)
        path = self.app.viewport.edit_path_text()
        self.panel.context.setText(tr("Editando: {path}", path=path) if path else tr("Modelo"))
        self.panel.leave.setEnabled(bool(path))
        self.panel.zoom.setEnabled(any(k in nodes and nodes[k].kind != "section" for k in self.wanted))
        matched = matching_keys(nodes, self.panel.search.text(), self.panel.kind.currentData())[1]
        count = len(nodes)
        self.panel.count.setText(tr("{shown} de {total} objetos · {selected} seleccionados",
                                    shown=len(matched), total=count, selected=len(self.wanted)))
        self.panel.empty.setVisible(not matched)
        self.panel.empty.setText(tr("No hay coincidencias. Cambie la búsqueda o el filtro.") if count
                                 else tr("Cree grupos o componentes para organizar el modelo."))

    def _rebuild(self, open_keys):
        expanded = {key for key, item in self.items.items() if item.isExpanded()}
        current = self.tree.currentItem()
        current_key = current.data(0, ROLE) if current else None
        scroll = self.tree.verticalScrollBar().value()
        included, _ = matching_keys(self.nodes, self.panel.search.text(), self.panel.kind.currentData())
        filtering = bool(self.panel.search.text()) or self.panel.kind.currentData() != "all"
        blocker = QSignalBlocker(self.tree)
        self.tree.clear()
        self.items = {}
        self._decorated = {}
        root = QTreeWidgetItem(self.tree, [tr("Modelo"), "", ""])
        root.setData(0, ROLE, ROOT_KEY)
        root.setIcon(0, icon("model"))
        root.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        root.setExpanded(True)
        sections = None
        siblings = {}
        for key, node in self.nodes.items():
            if key in included:
                siblings.setdefault(node.parent_key, []).append(node)
        if self.panel.sort.isChecked():
            for children in siblings.values():
                children.sort(key=lambda n: natural_key(n.name))
        stack = [(node, root) for node in reversed(siblings.get(None, []))]
        while stack:
            node, parent = stack.pop()
            if node.kind == "section":
                if sections is None:
                    sections = QTreeWidgetItem(root, [tr("Planos de sección"), "", ""])
                    sections.setData(0, ROLE, SECTIONS_KEY)
                    sections.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    sections.setIcon(0, icon("section"))
                    sections.setExpanded(True)
                parent = sections
            item = QTreeWidgetItem(parent, [node.name, node.tag, ""])
            item.setData(0, ROLE, node.key)
            self.decorate(item, node, open_keys)
            item.setExpanded(filtering or node.key in expanded or node.key in open_keys)
            self.items[node.key] = item
            stack.extend((child, item) for child in reversed(siblings.get(node.key, [])))
        if current_key in self.items:
            self.tree.setCurrentItem(self.items[current_key], 0,
                                     QItemSelectionModel.SelectionFlag.NoUpdate)
        self.tree.verticalScrollBar().setValue(scroll)
        del blocker

    def decorate(self, item, node, open_keys):
        # A refresh decorates every row; most rows have not changed, and the
        # icons, fonts and translated tooltips are what made it slow.
        state = (node.kind, node.name, node.tag, node.visible, node.locked,
                 bool(getattr(node.entity, "hidden", False)),
                 bool(getattr(node.entity, "active", False)),
                 node.key in open_keys,
                 tuple(g.name for g in node.ancestors),
                 self.panel.palette().cacheKey(), language())
        if self._decorated.get(id(item)) == state:
            return
        self._decorated[id(item)] = state
        item.setIcon(0, icon(node.kind))
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
        if node.kind != "section":
            flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            item.setIcon(2, icon("hidden" if not node.visible else "eye"))
            item.setIcon(1, icon("lock") if node.locked else QIcon())
            reason = (tr("Oculto directamente") if node.entity.hidden else
                      tr("Oculto por un contenedor o etiqueta") if not node.visible else tr("Visible"))
            item.setToolTip(2, reason + tr(". Clic: ocultar/mostrar este objeto."))
            item.setToolTip(1, node.tag + (tr(" · Etiqueta del objeto o antecesor bloqueada") if node.locked else ""))
        else:
            item.setIcon(2, icon("cut_on" if node.entity.active else "cut_off"))
            item.setToolTip(2, tr("Corte activo. Clic: desactivar.") if node.entity.active
                            else tr("Clic: activar este corte."))
        item.setFlags(flags)
        font = item.font(0)
        font.setItalic(not node.visible)
        font.setBold(node.key in open_keys or bool(getattr(node.entity, "active", False)))
        item.setFont(0, font)
        color = QPalette.ColorRole.Text if node.visible else QPalette.ColorRole.PlaceholderText
        item.setForeground(0, QBrush(self.panel.palette().color(color)))
        path = " ▸ ".join([g.name for g in node.ancestors] + [node.name])
        kind = {"group": tr("Grupo"), "component": tr("Componente"), "section": tr("Plano de sección")}[node.kind]
        item.setToolTip(0, f"{kind}\n{path}" + (tr("\nEtiqueta bloqueada") if node.locked else ""))

    def _sync_rows(self, reveal=False):
        blocker = QSignalBlocker(self.tree)
        for key, item in self.items.items():
            item.setSelected(key in self.wanted)
            if reveal and key in self.wanted:
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
        del blocker

    def selected_nodes(self):
        return [n for k, n in self.nodes.items() if k in self.wanted]

    def set_context(self, ancestors):
        vp = self.app.viewport
        opened = [level["group"] for level in vp.scene._edit_stack]
        common = 0
        for actual, desired in zip(opened, ancestors):
            if actual is not desired:
                break
            common += 1
        while len(vp.scene._edit_stack) > common:
            vp.end_one_group_edit()
        for group in ancestors[common:]:
            # In 0.5.7, entering a transformed container pushes a matrix
            # onto classic children. Their default component=True flag is
            # meaningless until that matrix exists: preserve their actual
            # group kind instead of turning them into components by opening.
            for child in group.children:
                if child.xform is None:
                    child.component = False
            vp.begin_group_edit(group)

    def close_contexts(self):
        self.set_context(())

    def tree_selection(self):
        if self._busy:
            return
        wanted = set(self.tree.selected_keys())
        nodes = [self.nodes[k] for k in wanted if k in self.nodes]
        visible = [n for n in nodes if n.visible and not n.locked]
        parents = {n.parent_key for n in visible}
        if len(parents) > 1:
            self._sync_rows()
            self.message(tr("Para selección múltiple, elija objetos del mismo contenedor."))
            return
        self._busy = True
        try:
            if visible:
                self.set_context(visible[0].ancestors)
            self.wanted = wanted
            self.app.scene.select([n.entity for n in visible])
            self._selection_stamp = self.selection_stamp()
            self.app.viewport.notify_scene_changed()
        finally:
            self._busy = False
        if nodes and not visible:
            self.message(tr("Objeto oculto o etiqueta bloqueada. Puede gestionarlo desde este árbol."))
        self.queue_refresh()

    def execute(self, command):
        if not getattr(command, "changed", True):
            self.queue_refresh()
            return False
        vp = self.app.viewport
        vp.history.execute(command)
        if vp.history.last_error:
            self.message(vp.history.last_error)
            return False
        # A tree selection can include hidden rows; retain them for Unhide.
        # When an operation shows/reparents them, reselect in the correct
        # native context so a highlighted row remains usable by Move, etc.
        nodes = index_scene(self.app.scene)
        self.wanted.intersection_update(nodes)
        visible = [n for key, n in nodes.items() if key in self.wanted
                   and n.visible and not n.locked]
        if len({n.parent_key for n in visible}) <= 1:
            was_busy = self._busy
            self._busy = True
            try:
                if visible:
                    self.set_context(visible[0].ancestors)
                self.app.scene.select([n.entity for n in visible])
            finally:
                self._busy = was_busy
        else:
            self.app.scene.clear_selection()
        self._selection_stamp = self.selection_stamp()
        vp.notify_scene_changed()
        self.queue_refresh()
        return True

    def item_changed(self, item, column):
        if column != 0 or self._busy:
            return
        key = item.data(0, ROLE)
        if key not in self.nodes:
            return
        name = item.text(0)
        self._busy = True
        try:
            self.close_contexts()
            self.execute(Rename(self.nodes[key].entity, name))
        except ValueError as exc:
            self.message(exc)
        finally:
            self._busy = False
            self._force = True
            self.queue_refresh()

    def rename(self):
        selected = [self.items[k] for k in self.wanted if k in self.items]
        if len(selected) == 1:
            self.tree.editItem(selected[0], 0)
        else:
            self.message(tr("Seleccione un objeto para renombrarlo."))

    def item_clicked(self, item, column):
        if column == 2:
            self.toggle(item.data(0, ROLE))

    def double_clicked(self, item, column):
        if column == 0:
            self.edit(item.data(0, ROLE))

    def toggle(self, key):
        node = self.nodes.get(key)
        if node is None:
            return
        self.close_contexts()
        if node.kind == "section":
            self.execute(SetActiveSectionCommand(None if node.entity.active else node.entity))
        else:
            hidden = not node.entity.hidden
            self.execute(HideCommand([node.entity], hidden))
            if not hidden and not index_scene(self.app.scene)[key].visible:
                self.message(tr("El objeto sigue oculto por un contenedor o una etiqueta."))

    def visibility(self, hidden):
        entities = [n.entity for n in self.selected_nodes() if n.kind != "section"
                    and n.entity.hidden != hidden]
        if entities:
            self.close_contexts()
            self.execute(HideCommand(entities, hidden))

    def edit(self, key=None):
        if not isinstance(key, str):
            key = next(iter(self.wanted)) if len(self.wanted) == 1 else None
        node = self.nodes.get(key)
        if node is None:
            self.message(tr("Seleccione un objeto para editarlo."))
            return
        if node.kind == "section":
            self.toggle(key)
            return
        if not node.visible or node.locked:
            self.message(tr("Muestre el objeto y desbloquee su etiqueta antes de editarlo."))
            return
        self.set_context(node.ancestors + (node.entity,))
        self.wanted.clear()
        self._selection_stamp = self.selection_stamp()
        self.app.viewport.notify_scene_changed()
        self.queue_refresh()
        self.app.viewport.setFocus()

    def leave(self):
        self.app.viewport.end_one_group_edit()
        self.wanted.clear()
        self._selection_stamp = self.selection_stamp()
        self.queue_refresh()

    def zoom(self):
        low, high = bounds_of(index_scene(self.app.scene), self.wanted)
        if low is None:
            self.message(tr("La selección no contiene geometría para encuadrar."))
            return
        self.app.viewport.camera.fit_box(low, high)
        self.app.viewport.update()

    def locate(self):
        keys = {entity_key(e) for e in self.app.scene.selection if hasattr(e, "uid")}
        self.panel.search.clear()
        self.panel.kind.setCurrentIndex(0)
        self.wanted = keys
        self._selection_stamp = self.selection_stamp()
        self.refresh()
        self._sync_rows(reveal=True)
        first = next((self.items[k] for k in keys if k in self.items), None)
        if first is not None:
            self.tree.scrollToItem(first)

    def collapse(self):
        self.tree.collapseAll()
        if self.tree.topLevelItemCount():
            self.tree.topLevelItem(0).setExpanded(True)

    def move(self, keys, destination=None, before=None):
        try:
            self.close_contexts()
            nodes = index_scene(self.app.scene)
            position = None
            if before is not None:
                if self.panel.sort.isChecked():
                    raise ValueError(tr("Desactive el orden A–Z para reordenar entre hermanos."))
                sibling = nodes.get(before[0])
                if sibling is None:
                    raise ValueError(tr("El objeto de destino ya no existe."))
                owner = sibling.parent.children if sibling.parent else self.app.scene.groups
                position = owner.index(sibling.entity) + int(before[1])
            command = Reparent(self.app.scene, keys, destination, position)
            self.wanted = {n.key for n in command.moving}
            if self.execute(command):
                self.message(tr("Jerarquía actualizada. Los objetos conservan su posición 3D."))
        except ValueError as exc:
            self.message(exc)

    def assign_tag(self):
        nodes = [n for n in self.selected_nodes() if n.kind != "section"]
        if not nodes:
            return
        tags = [layer.name for layer in self.app.scene.layers]
        dialog = QInputDialog(self.panel)
        dialog.setWindowTitle(tr("Asignar etiqueta"))
        dialog.setLabelText(tr("Etiqueta:"))
        dialog.setComboBoxItems(tags)
        dialog.setComboBoxEditable(False)
        dialog.setOkButtonText(tr("Aceptar"))
        dialog.setCancelButtonText(tr("Cancelar"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            tag = dialog.textValue()
            self.close_contexts()
            self.execute(AssignLayerCommand([n.entity for n in nodes], tag or DEFAULT_LAYER))

    def delete(self):
        self.close_contexts()
        nodes = index_scene(self.app.scene)
        groups = topmost(nodes, self.wanted)
        if any(n.locked for n in groups):
            self.message(tr("Desbloquee las etiquetas antes de eliminar estos objetos."))
            return
        commands = [DeleteGroupCommand(n.entity) for n in groups]
        planes = [nodes[k].entity for k in self.wanted if k in nodes and nodes[k].kind == "section"]
        if planes:
            commands.append(DeleteSectionPlanesCommand(planes))
        if commands:
            self.execute(CompoundCommand(commands))

    def context_menu(self, position):
        item = self.tree.itemAt(position)
        if item is not None and item.data(0, ROLE) in self.nodes and not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        menu = QMenu(self.tree)
        if self.wanted:
            menu.addAction(tr("Renombrar · F2"), self.rename).setEnabled(len(self.wanted) == 1)
            menu.addAction(tr("Editar · Intro"), self.edit).setEnabled(len(self.wanted) == 1)
            menu.addAction(tr("Encuadrar · F"), self.zoom)
            menu.addSeparator()
            if any(n.kind != "section" for n in self.selected_nodes()):
                menu.addAction(tr("Ocultar"), lambda: self.visibility(True))
                menu.addAction(tr("Mostrar"), lambda: self.visibility(False))
                menu.addAction(tr("Asignar etiqueta…"), self.assign_tag)
                menu.addAction(tr("Mover al nivel del modelo"), lambda: self.move(list(self.wanted)))
                menu.addSeparator()
            menu.addAction(tr("Eliminar"), self.delete)
            menu.addSeparator()
        menu.addAction(tr("Expandir todo"), self.tree.expandAll)
        menu.addAction(tr("Contraer todo"), self.collapse)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def viewport_menu(self, menu, selection):
        if any(hasattr(e, "uid") for e in selection):
            action = menu.addAction(tr("Localizar en JA Tree"))
            action.triggered.connect(lambda: QTimer.singleShot(0, self.show_and_locate))

    def show_and_locate(self):
        self.show()
        self.locate()
