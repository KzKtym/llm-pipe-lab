/* 一覧から比較画面への導線。
 *
 * 比較は2件でしか成立しない。3件以上選ばれた状態で遷移させると、
 * 比較画面側で「どの2件か」を勝手に決めることになるので、ここで止める。
 * 遷移は GET のみ（この画面に書き込み操作は無い）。
 */
document.addEventListener('DOMContentLoaded', function () {
    var config = document.getElementById('jsConfig');
    if (!config) { return; }
    var compareUrl = config.dataset.compareUrl;
    var hint = document.getElementById('selectHint');
    var button = document.getElementById('compareBtn');

    function checked() {
        return Array.prototype.slice
            .call(document.querySelectorAll('.exp-check'))
            .filter(function (box) { return box.checked; });
    }

    function refresh() {
        var count = checked().length;
        if (!hint) { return; }
        if (count === 2) {
            hint.textContent = '2件選択中';
            hint.className = 'text-success text-small fw-bold';
        } else if (count > 2) {
            hint.textContent = '選べるのは2件までです（現在 ' + count + '件）';
            hint.className = 'text-danger text-small fw-bold';
        } else {
            hint.textContent = '2件選ぶと比較できます';
            hint.className = 'text-muted text-small';
        }
    }

    document.querySelectorAll('.exp-check').forEach(function (box) {
        box.addEventListener('change', refresh);
    });

    if (button) {
        button.addEventListener('click', function () {
            var selected = checked();
            if (selected.length !== 2) {
                alert('比較する実験を2件選んでください（現在 ' + selected.length + '件）');
                return;
            }
            // 若い方を A に置く。左右が選択順で入れ替わると差分の符号が読みにくい
            var ids = selected.map(function (box) { return parseInt(box.value, 10); })
                              .sort(function (a, b) { return a - b; });
            window.location.href = compareUrl + '?a=' + ids[0] + '&b=' + ids[1];
        });
    }

    refresh();
});
