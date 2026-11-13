
use super::{Request, apply, rd};

#[test]
fn rounding_is_half_away_from_zero() {
    assert_eq!(rd(1, 2).unwrap(), 1);
    assert_eq!(rd(-1, 2).unwrap(), -1);
    assert_eq!(rd(5, 2).unwrap(), 3);
    assert_eq!(rd(-5, 2).unwrap(), -3);
    assert_eq!(rd(1, 0).unwrap_err().message, "positive divisor");
}

#[test]
fn identity_and_amount_zero_do_not_touch_the_other_entry() {
    let mut stock = vec![0u8; 552];
    let mut base = vec![7u8; 552];
    stock[0] = 4;
    base[0] = 4;
    stock[1] = 0x17;
    base[1] = 0x17;
    let (out, stats) = apply(&stock, &base, &Request::identity()).unwrap();
    assert_eq!(out, base);
    assert_eq!(stats.clamps, 0);
    let mut req = Request::identity();
    req.amount = 0;
    let stock_before = stock.clone();
    let (out, _) = apply(&stock, &base, &req).unwrap();
    assert_eq!(stock, stock_before);
    assert_eq!(out.len(), 552);
    assert_ne!(out, stock);
    req.amount = 151;
    assert_eq!(apply(&stock, &base, &req).unwrap_err().message, "amount");
    req.amount = 100;
    req.doses[3] = -1;
    assert_eq!(
        apply(&stock, &base, &req).unwrap_err().message,
        "fade is one-way"
    );
    assert_eq!(
        apply(&[0], &base, &Request::identity())
            .unwrap_err()
            .message,
        "entry length"
    );
}
