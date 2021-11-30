from enum import Enum
from typing import Optional, Sequence

from PyQt5 import QtCore, QtWidgets

from electroncash.address import Address, AddressError
from electroncash.consolidate import (
    MAX_STANDARD_TX_SIZE,
    MAX_TX_SIZE,
    AddressConsolidator,
)

from electroncash.transaction import Transaction
from electroncash.wallet import Abstract_Wallet
from electroncash_gui.qt.multi_transactions_dialog import MultiTransactionsWidget

unit = "BCH"
sats_to_BCH_conv_factor = 100000000

class TransactionsStatus(Enum):
    INTERRUPTED = "cancelled"
    NOT_STARTED = "not started"
    SELECTING = "selecting coins..."
    BUILDING = "building transactions..."
    FINISHED = "finished building transactions"
    NO_RESULT = "finished without generating any transactions"


class ConsolidateWorker(QtCore.QObject):
    finished = QtCore.pyqtSignal()
    status_changed = QtCore.pyqtSignal(TransactionsStatus)
    transactions_ready = QtCore.pyqtSignal(list)
    """Emits the list of :class:`Transaction` after the last transaction is
     generated."""
    progress = QtCore.pyqtSignal(int)
    """Emits the number of generated transactions after each new transaction."""

    def __init__(
        self,
        address: Address,
        wallet: Abstract_Wallet,
        include_coinbase: bool,
        include_non_coinbase: bool,
        include_frozen: bool,
        include_slp: bool,
        minimum_value: Optional[int],
        maximum_value: Optional[int],
        output_address: Address,
        max_tx_size: int,
    ):
        super().__init__()
        self.status_changed.emit(TransactionsStatus.SELECTING)
        self.consolidator = AddressConsolidator(
            address,
            wallet,
            include_coinbase,
            include_non_coinbase,
            include_frozen,
            include_slp,
            minimum_value,
            maximum_value,
            output_address,
            max_tx_size,
        )

        self.interrupt_mutex = QtCore.QMutex()
        self.interrupt: bool = False

    def was_interruption_requested(self) -> bool:
        self.interrupt_mutex.lock()
        do_interrupt = self.interrupt
        self.interrupt_mutex.unlock()
        return do_interrupt

    def request_interruption(self):
        """Stop the worker as soon as possible (i.e. in-between two
        transactions).
        This causes the :attr:`status_changed` and :attr:`finished` signals to be
        emitted. The :attr:`transactions_ready` signal is not emitted if the worker
        is interrupted before it has generated the last transaction.
        """
        self.interrupt_mutex.lock()
        self.interrupt = True
        self.interrupt_mutex.unlock()

    def build_transactions(self):
        self.status_changed.emit(TransactionsStatus.BUILDING)
        transactions = []
        for i, tx in enumerate(self.consolidator.iter_transactions()):
            if self.was_interruption_requested():
                self.status_changed.emit(TransactionsStatus.INTERRUPTED)
                self.finished.emit()
                return
            transactions.append(tx)
            self.progress.emit(i + 1)

        if transactions:
            self.status_changed.emit(TransactionsStatus.FINISHED)
            # else the transaction page will set the status to NO_RESULT upon receiving
            # an empty list of transactions
        self.transactions_ready.emit(transactions)
        self.finished.emit()


class ConsolidateCoinsWizard(QtWidgets.QWizard):
    def __init__(
        self,
        address: Address,
        wallet: Abstract_Wallet,
        main_window,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            f"Consolidate coins for address {address.to_full_ui_string()}"
        )

        self.tx_thread: Optional[QtCore.QThread] = None

        self.address: Address = address
        self.wallet: Abstract_Wallet = wallet
        self.transactions: Sequence[Transaction] = []

        self.coins_page = CoinSelectionPage()
        self.addPage(self.coins_page)

        self.output_page = OutputsPage(address)
        self.addPage(self.output_page)

        self.tx_page = TransactionsPage(wallet, main_window)
        self.addPage(self.tx_page)

        self.currentIdChanged.connect(self.on_page_changed)

    def on_page_changed(self, page_id: int):
        # The thread is only supposed to be started after reaching the tx_page,
        # and must be stopped if the user decides to go back to a previous page
        # or close the dialog.
        self.stop_thread_if_running()

        if self.currentPage() is self.tx_page:
            self.tx_page.update_status(TransactionsStatus.NOT_STARTED)
            self.tx_thread = QtCore.QThread()
            self.worker = ConsolidateWorker(
                self.address,
                self.wallet,
                self.coins_page.include_coinbase_cb.isChecked(),
                self.coins_page.include_non_coinbase_cb.isChecked(),
                self.coins_page.include_frozen_cb.isChecked(),
                self.coins_page.include_slp_cb.isChecked(),
                self.coins_page.get_minimum_value(),
                self.coins_page.get_maximum_value(),
                self.output_page.get_output_address(),
                self.output_page.tx_size_sb.value(),
            )
            # Connections
            self.worker.moveToThread(self.tx_thread)
            self.tx_thread.started.connect(self.worker.build_transactions)
            self.worker.status_changed.connect(self.tx_page.update_status)
            self.worker.progress.connect(self.tx_page.update_progress)
            self.worker.transactions_ready.connect(self.on_build_transactions_finished)
            self.worker.finished.connect(self.tx_thread.quit)

            self.tx_thread.start()

    def stop_thread_if_running(self):
        if self.tx_thread is not None and self.tx_thread.isRunning():
            self.worker.request_interruption()
            self.tx_thread.quit()

    def on_build_transactions_finished(self, transactions: Sequence[Transaction]):
        self.transactions = transactions
        self.tx_page.set_unsigned_transactions(self.transactions)


class CoinSelectionPage(QtWidgets.QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Filter coins")

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        self.include_coinbase_cb = QtWidgets.QCheckBox("Include coinbase coins")
        self.include_coinbase_cb.setChecked(True)
        layout.addWidget(self.include_coinbase_cb)

        self.include_non_coinbase_cb = QtWidgets.QCheckBox("Include non-coinbase coins")
        self.include_non_coinbase_cb.setChecked(True)
        layout.addWidget(self.include_non_coinbase_cb)

        self.include_frozen_cb = QtWidgets.QCheckBox("Include frozen coins")
        self.include_frozen_cb.setChecked(False)
        layout.addWidget(self.include_frozen_cb)

        self.include_slp_cb = QtWidgets.QCheckBox("Include coins with SLP tokens")
        self.include_slp_cb.setChecked(False)
        self.include_slp_cb.toggled.connect(self.warn_burn_tokens)
        layout.addWidget(self.include_slp_cb)

        min_value_sublayout = QtWidgets.QHBoxLayout()
        layout.addLayout(min_value_sublayout)
        self.filter_by_min_value_cb = QtWidgets.QCheckBox(
            "Define a minimum value for coins to select"
        )
        self.filter_by_min_value_cb.setChecked(False)
        min_value_sublayout.addWidget(self.filter_by_min_value_cb)

        self.minimum_value_sb = QtWidgets.QDoubleSpinBox()
        self.minimum_value_sb.setDecimals(8)
        self.minimum_value_sb.setEnabled(False)
        self.minimum_value_sb.setStepType(
            QtWidgets.QAbstractSpinBox.AdaptiveDecimalStepType
        )
        self.minimum_value_sb.setMaximum(21_000_000)
        self.minimum_value_sb.setValue(0.00000546)
        self.minimum_value_sb.setToolTip(f"{unit}")
        self.filter_by_min_value_cb.toggled.connect(self.minimum_value_sb.setEnabled)
        min_value_sublayout.addWidget(self.minimum_value_sb)

        max_value_sublayout = QtWidgets.QHBoxLayout()
        layout.addLayout(max_value_sublayout)
        self.filter_by_max_value_cb = QtWidgets.QCheckBox(
            "Define a maximum value for coins to select"
        )
        self.filter_by_max_value_cb.setChecked(False)
        max_value_sublayout.addWidget(self.filter_by_max_value_cb)

        self.maximum_value_sb = QtWidgets.QDoubleSpinBox()
        self.maximum_value_sb.setDecimals(8)
        self.maximum_value_sb.setEnabled(False)
        self.maximum_value_sb.setStepType(
            QtWidgets.QAbstractSpinBox.AdaptiveDecimalStepType
        )
        self.maximum_value_sb.setMaximum(21_000_000)
        self.maximum_value_sb.setValue(21_000_000)
        self.maximum_value_sb.setToolTip(f"{unit}")
        self.filter_by_max_value_cb.toggled.connect(self.maximum_value_sb.setEnabled)
        max_value_sublayout.addWidget(self.maximum_value_sb)

    def warn_burn_tokens(self, include_slp_is_checked: bool):
        if include_slp_is_checked:
            button = QtWidgets.QMessageBox.warning(
                self,
                "SLP tokens may be lost",
                f"{PROJECT_NAME} does not support transferring SLP tokens. If you "
                "include them in the consolidation transaction, they will be burned.",
                buttons=QtWidgets.QMessageBox.Cancel | QtWidgets.QMessageBox.Ok,
            )
            if button == QtWidgets.QMessageBox.Cancel:
                self.include_slp_cb.setChecked(False)

    def get_minimum_value(self) -> Optional[int]:
        """Return minimum value in satoshis, or None"""
        return (
            None
            if not self.filter_by_min_value_cb.isChecked()
            else int(sats_to_BCH_conv_factor * self.minimum_value_sb.value())
        )

    def get_maximum_value(self) -> Optional[int]:
        """Return maximum value in satoshis, or None"""
        return (
            None
            if not self.filter_by_max_value_cb.isChecked()
            else int(sats_to_BCH_conv_factor * self.maximum_value_sb.value())
        )


class OutputsPage(QtWidgets.QWizardPage):
    def __init__(self, input_address: Address, parent=None):
        super().__init__(parent)

        self.inputs_address: Address = input_address
        self.output_address: Optional[Address] = None

        self.setTitle("Outputs")

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        layout.addWidget(QtWidgets.QLabel("<h2>Destination address</h2>"))
        self.same_address_rb = QtWidgets.QRadioButton("Same address as inputs")
        self.same_address_rb.setChecked(True)
        layout.addWidget(self.same_address_rb)

        single_address_sublayout = QtWidgets.QHBoxLayout()
        layout.addLayout(single_address_sublayout)
        self.single_address_rb = QtWidgets.QRadioButton("Single address")
        single_address_sublayout.addWidget(self.single_address_rb)

        self.output_address_edit = QtWidgets.QLineEdit()
        self.output_address_edit.setPlaceholderText("enter a valid destination address")
        self.output_address_edit.setEnabled(False)
        single_address_sublayout.addWidget(self.output_address_edit)

        layout.addSpacing(20)

        layout.addWidget(QtWidgets.QLabel("<h2>Transaction parameters</h2>"))
        tx_size_layout = QtWidgets.QHBoxLayout()
        layout.addLayout(tx_size_layout)
        tx_size_layout.addWidget(QtWidgets.QLabel("Maximum transaction size (bytes)"))
        self.tx_size_sb = QtWidgets.QSpinBox()
        self.tx_size_sb.setMinimum(192)
        self.tx_size_sb.setMaximum(MAX_TX_SIZE)
        self.tx_size_sb.setValue(MAX_STANDARD_TX_SIZE)
        tx_size_layout.addWidget(self.tx_size_sb)

        self.single_address_rb.toggled.connect(self.output_address_edit.setEnabled)
        self.single_address_rb.toggled.connect(self.completeChanged.emit)
        self.output_address_edit.textChanged.connect(self.validate_address)

    def validate_address(self, address_text: str):
        previous_address = self.output_address
        try:
            self.output_address = Address.from_string(address_text)
        except AddressError:
            self.output_address = None
        if self.output_address != previous_address:
            self.completeChanged.emit()

    def isComplete(self):
        return not self.single_address_rb.isChecked() or self.output_address is not None

    def get_output_address(self) -> Address:
        return (
            self.inputs_address
            if self.same_address_rb.isChecked()
            else self.output_address
        )


class TransactionsPage(QtWidgets.QWizardPage):
    def __init__(self, wallet, main_window, parent=None):
        super().__init__(parent)
        self.status: TransactionsStatus = TransactionsStatus.NOT_STARTED
        self.setTitle("Transactions")

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)

        self.status_label = QtWidgets.QLabel()
        layout.addWidget(self.status_label)

        self.multi_tx_display = MultiTransactionsWidget(wallet, main_window)
        layout.addWidget(self.multi_tx_display)

    def display_work_in_progress(self):
        """Disable buttons, inform the user about the ongoing computation"""
        self.multi_tx_display.reset_labels()
        self.multi_tx_display.disable_buttons()
        self.setCursor(QtCore.Qt.WaitCursor)

    def update_status(self, status: TransactionsStatus):
        if status == TransactionsStatus.BUILDING:
            self.display_work_in_progress()
        self.status_label.setText(f"Status: <b>{status.value}</b>")

        previous_status, self.status = self.status, status
        if previous_status != status and TransactionsStatus.FINISHED in [
            previous_status,
            status,
        ]:
            self.completeChanged.emit()

    def update_progress(self, num_tx: int):
        self.multi_tx_display.set_displayed_number_of_transactions(num_tx)

    def set_unsigned_transactions(self, transactions: Sequence[Transaction]):
        self.unsetCursor()
        if not transactions:
            self.update_status(TransactionsStatus.NO_RESULT)
            return
        self.multi_tx_display.set_transactions(transactions)

    def isComplete(self) -> bool:
        return self.status == TransactionsStatus.FINISHED
