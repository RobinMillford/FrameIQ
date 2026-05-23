document.addEventListener('DOMContentLoaded', function() {
    const passwordInput = document.getElementById('password');
    const lengthReq = document.getElementById('length-req');
    const uppercaseReq = document.getElementById('uppercase-req');
    const lowercaseReq = document.getElementById('lowercase-req');
    const numberReq = document.getElementById('number-req');

    passwordInput.addEventListener('input', function() {
        const password = passwordInput.value;

        const checks = [
            [lengthReq, password.length >= 8],
            [uppercaseReq, /[A-Z]/.test(password)],
            [lowercaseReq, /[a-z]/.test(password)],
            [numberReq, /\d/.test(password)],
        ];

        for (const [el, met] of checks) {
            el.classList.toggle('requirement-met', met);
            el.classList.toggle('requirement-not-met', !met);
        }
    });
});
